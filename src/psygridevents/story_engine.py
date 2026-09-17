from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .acquisition import RSSAcquirer, RawObservation
from .clustering import StoryCluster, cluster_stories
from .deduplication import deduplicate
from .entity_resolution import InstrumentResolver, EntityMatch
from .evidence import EvidenceAssessment, assess_evidence
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


class StoryEngine:
    """Acquisition-to-semantic-event pipeline with explicit provenance."""

    def __init__(
        self,
        instrument_file: str | Path,
        semantic_rules_file: str | Path | None = None,
        materiality_rules_file: str | Path | None = None,
        exposure_rules_file: str | Path | None = None,
        issuer_metadata_file: str | Path | None = None,
    ) -> None:
        self.acquirer = RSSAcquirer()
        self.resolver = InstrumentResolver.from_instrument_file(instrument_file)
        if semantic_rules_file is None:
            semantic_rules_file = Path(instrument_file).parent / "semantic_rules.yaml"
        if materiality_rules_file is None:
            materiality_rules_file = Path(instrument_file).parent / "materiality_rules.yaml"
        if exposure_rules_file is None:
            exposure_rules_file = Path(instrument_file).parent / "exposure_rules.yaml"
        self.semantic_extractor = SemanticExtractor(semantic_rules_file)
        self.novelty_engine = NoveltyEngine()
        self.materiality_engine = MaterialityEngine(materiality_rules_file)
        self.transmission_engine = TransmissionEngine(exposure_rules_file, issuer_metadata_file)

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
            for event in item.semantic_events:
                assessment = self.novelty_engine.assess(event, historical_events, as_of=as_of)
                updated_events.append(
                    replace(
                        event,
                        novelty_status=assessment.status,
                        novelty_score=assessment.score,
                        novelty_reason=assessment.reason,
                    )
                )
            assessed.append(replace(item, semantic_events=tuple(updated_events)))
        return assessed

    def build_materiality(
        self,
        intelligence: list[StoryIntelligence],
    ) -> list[StoryIntelligence]:
        """Attach evidence-gated materiality to already extracted events."""
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

    def build_transmission(
        self,
        intelligence: list[StoryIntelligence],
    ) -> list[StoryIntelligence]:
        """Attach explicit exposure/transmission paths without inventing links."""
        result: list[StoryIntelligence] = []
        for item in intelligence:
            transmissions = self.transmission_engine.assess_many(item.semantic_events)
            result.append(replace(item, transmissions=transmissions))
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
