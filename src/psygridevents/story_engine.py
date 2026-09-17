from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .acquisition import RSSAcquirer, RawObservation
from .clustering import StoryCluster, cluster_stories
from .contradiction import ContradictionAssessment, ContradictionEngine
from .deduplication import deduplicate
from .entity_resolution import InstrumentResolver, EntityMatch
from .evidence import EvidenceAssessment, assess_evidence
from .market_confirmation import MarketConfirmationAssessment, MarketConfirmationEngine, MarketObservation
from .materiality import MaterialityEngine
from .normalize import normalized_observation
from .novelty import NoveltyEngine
from .semantic import SemanticEvent, SemanticExtractor
from .transmission import TransmissionAssessment, TransmissionEngine


@dataclass(frozen=True)
class StoryIntelligence:
    story: StoryCluster
    entities: tuple[EntityMatch, ...]
    evidence: EvidenceAssessment
    semantic_events: tuple[SemanticEvent, ...] = ()
    transmissions: tuple[TransmissionAssessment, ...] = ()
    contradictions: tuple[ContradictionAssessment, ...] = ()
    market_confirmations: tuple[MarketConfirmationAssessment, ...] = ()


class StoryEngine:
    """Acquisition-to-semantic-event pipeline with explicit provenance."""

    def __init__(
        self,
        instrument_file: str | Path,
        semantic_rules_file: str | Path | None = None,
        materiality_rules_file: str | Path | None = None,
        exposure_rules_file: str | Path | None = None,
        issuer_metadata_file: str | Path | None = None,
        contradiction_rules_file: str | Path | None = None,
        market_confirmation_rules_file: str | Path | None = None,
    ) -> None:
        self.acquirer = RSSAcquirer()
        self.resolver = InstrumentResolver.from_instrument_file(instrument_file)
        config_dir = Path(instrument_file).parent
        if semantic_rules_file is None:
            semantic_rules_file = config_dir / "semantic_rules.yaml"
        if materiality_rules_file is None:
            materiality_rules_file = config_dir / "materiality_rules.yaml"
        if exposure_rules_file is None:
            exposure_rules_file = config_dir / "exposure_rules.yaml"
        if contradiction_rules_file is None:
            contradiction_rules_file = config_dir / "contradiction_rules.yaml"
        if market_confirmation_rules_file is None:
            market_confirmation_rules_file = config_dir / "market_confirmation_rules.yaml"
        self.semantic_extractor = SemanticExtractor(semantic_rules_file)
        self.novelty_engine = NoveltyEngine()
        self.materiality_engine = MaterialityEngine(materiality_rules_file)
        self.transmission_engine = TransmissionEngine(exposure_rules_file, issuer_metadata_file)
        self.contradiction_engine = ContradictionEngine(contradiction_rules_file)
        self.market_confirmation_engine = MarketConfirmationEngine(market_confirmation_rules_file)

    def acquire(self, feeds: list[dict], since: datetime | None = None) -> list[RawObservation]:
        observations: list[RawObservation] = []
        for feed in feeds:
            if not feed.get("enabled") or feed.get("mode") == "catalogue":
                continue
            observations.extend(
                self.acquirer.fetch(
                    provider_id=feed["provider_id"],
                    publisher=feed["publisher"],
                    url=feed["url"],
                    source_tier=int(feed["tier"]),
                    since=since,
                )
            )
        return [normalized_observation(item) for item in observations]

    def build_stories(
        self,
        observations: list[RawObservation],
        historical_events: tuple[SemanticEvent, ...] = (),
        *,
        as_of: datetime | None = None,
    ) -> list[StoryIntelligence]:
        decisions = deduplicate(observations)
        unique = [decision.observation for decision in decisions if decision.duplicate_of is None]
        stories = cluster_stories(unique)
        intelligence = [self._intelligence(story) for story in stories]
        if not historical_events:
            return intelligence

        assessed: list[StoryIntelligence] = []
        for item in intelligence:
            updated_events: list[SemanticEvent] = []
            contradictions: list[ContradictionAssessment] = []
            for event in item.semantic_events:
                novelty = self.novelty_engine.assess(event, historical_events, as_of=as_of)
                contradiction = self.contradiction_engine.assess(event, historical_events)
                contradictions.append(contradiction)
                updated_events.append(
                    replace(
                        event,
                        novelty_status=novelty.status,
                        novelty_score=novelty.score,
                        novelty_reason=novelty.reason,
                        contradiction_status=contradiction.status,
                        narrative_state=contradiction.narrative_state,
                        contradiction_score=contradiction.similarity,
                        contradiction_reason=contradiction.reason,
                    )
                )
            assessed.append(
                replace(
                    item,
                    semantic_events=tuple(updated_events),
                    contradictions=tuple(contradictions),
                )
            )
        return assessed

    def build_materiality(self, intelligence: list[StoryIntelligence]) -> list[StoryIntelligence]:
        result: list[StoryIntelligence] = []
        for item in intelligence:
            events = []
            for event in item.semantic_events:
                assessment = self.materiality_engine.assess(event)
                events.append(
                    replace(
                        event,
                        materiality_status=assessment.status,
                        materiality_score=assessment.score,
                        materiality_reason=assessment.reason,
                    )
                )
            result.append(replace(item, semantic_events=tuple(events)))
        return result

    def build_transmission(self, intelligence: list[StoryIntelligence]) -> list[StoryIntelligence]:
        result: list[StoryIntelligence] = []
        for item in intelligence:
            transmissions = self.transmission_engine.assess_many(item.semantic_events)
            result.append(replace(item, transmissions=transmissions))
        return result

    def build_contradiction(
        self,
        intelligence: list[StoryIntelligence],
        historical_events: tuple[SemanticEvent, ...],
    ) -> list[StoryIntelligence]:
        result: list[StoryIntelligence] = []
        for item in intelligence:
            assessments = self.contradiction_engine.assess_many(item.semantic_events, historical_events)
            events = [
                replace(
                    event,
                    contradiction_status=assessment.status,
                    narrative_state=assessment.narrative_state,
                    contradiction_score=assessment.similarity,
                    contradiction_reason=assessment.reason,
                )
                for event, assessment in zip(item.semantic_events, assessments)
            ]
            result.append(replace(item, semantic_events=tuple(events), contradictions=assessments))
        return result

    def build_market_confirmation(
        self,
        intelligence: list[StoryIntelligence],
        market_observations: Iterable[MarketObservation],
    ) -> list[StoryIntelligence]:
        """Attach synchronized market reaction without inventing missing observations."""
        observations = tuple(market_observations)
        result: list[StoryIntelligence] = []
        for item in intelligence:
            assessments = self.market_confirmation_engine.assess_many(item.semantic_events, observations)
            events = [
                replace(
                    event,
                    market_confirmation_status=assessment.status,
                    market_confirmation_score=assessment.score,
                    market_confirmation_reason=assessment.reason,
                )
                for event, assessment in zip(item.semantic_events, assessments)
            ]
            result.append(
                replace(
                    item,
                    semantic_events=tuple(events),
                    market_confirmations=assessments,
                )
            )
        return result

    def _intelligence(self, story: StoryCluster) -> StoryIntelligence:
        text = " ".join(f"{item.title} {item.summary}" for item in story.observations)
        matches = tuple(self.resolver.resolve(text))
        evidence = assess_evidence(list(story.observations))
        base = StoryIntelligence(story=story, entities=matches, evidence=evidence)
        events = tuple(self.semantic_extractor.extract(base))
        transmissions = self.transmission_engine.assess_many(events)
        return StoryIntelligence(
            story=story,
            entities=matches,
            evidence=evidence,
            semantic_events=events,
            transmissions=transmissions,
        )

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
