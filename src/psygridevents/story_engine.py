from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .acquisition import RSSAcquirer, RawObservation
from .asset_mechanism import AssetMechanismEngine, AssetMechanismMapping
from .clustering import StoryCluster, cluster_stories
from .contradiction import ContradictionAssessment, ContradictionEngine
from .deduplication import deduplicate
from .entity_resolution import InstrumentResolver, EntityMatch
from .evidence import EvidenceAssessment, assess_evidence
from .event_timing import EventTimingAssessment, EventTimingEngine
from .exhaustion import ExhaustionAssessment, ExhaustionEngine
from .market_confirmation import (
    MarketConfirmationAssessment,
    MarketConfirmationEngine,
    MarketObservation,
)
from .market_response import MarketResponseAssessment, MarketResponseEngine
from .materiality import MaterialityEngine
from .normalize import normalized_observation
from .novelty import NoveltyEngine
from .priority import PriorityAssessment, PriorityEngine
from .semantic import SemanticEvent, SemanticExtractor
from .signal_engine import SignalAssessment, SignalEngine
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
    priorities: tuple[PriorityAssessment, ...] = ()
    asset_mechanisms: tuple[tuple[AssetMechanismMapping, ...], ...] = ()
    event_timings: tuple[EventTimingAssessment, ...] = ()
    market_responses: tuple[MarketResponseAssessment | None, ...] = ()
    exhaustions: tuple[ExhaustionAssessment | None, ...] = ()
    signals: tuple[SignalAssessment, ...] = ()


@dataclass(frozen=True)
class ProviderAcquisitionStatus:
    """One provider's outcome for a single acquire() call -- health/diagnostics only."""

    provider_id: str
    publisher: str
    attempted_at: datetime
    success: bool
    observation_count: int
    error: str | None


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
        priority_rules_file: str | Path | None = None,
        direction_rules_file: str | Path | None = None,
        macro_exposure_rules_file: str | Path | None = None,
        event_timing_rules_file: str | Path | None = None,
        market_response_rules_file: str | Path | None = None,
        issuer_records: tuple | None = None,
    ) -> None:
        self.acquirer = RSSAcquirer()
        self.last_acquisition_status: dict[str, ProviderAcquisitionStatus] = {}
        self.resolver = InstrumentResolver.from_instrument_file(instrument_file)
        if issuer_records:
            # CP8: extend entity resolution with verified issuer names/aliases
            # only. Unverified rows never enter resolution (see
            # InstrumentResolver.from_issuer_records / IssuerMasterBuilder).
            self.resolver = InstrumentResolver.from_issuer_records(issuer_records, self.resolver)
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
        if priority_rules_file is None:
            priority_rules_file = config_dir / "priority_rules.yaml"
        if direction_rules_file is None:
            direction_rules_file = config_dir / "direction_rules.yaml"
        if macro_exposure_rules_file is None:
            macro_exposure_rules_file = config_dir / "macro_exposure_rules.yaml"
        if event_timing_rules_file is None:
            event_timing_rules_file = config_dir / "event_timing_rules.yaml"
        if market_response_rules_file is None:
            market_response_rules_file = config_dir / "market_response_rules.yaml"
        self.semantic_extractor = SemanticExtractor(semantic_rules_file)
        self.novelty_engine = NoveltyEngine()
        self.materiality_engine = MaterialityEngine(materiality_rules_file)
        self.transmission_engine = TransmissionEngine(exposure_rules_file, issuer_metadata_file)
        self.contradiction_engine = ContradictionEngine(contradiction_rules_file)
        self.market_confirmation_engine = MarketConfirmationEngine(market_confirmation_rules_file)
        self.priority_engine = PriorityEngine(priority_rules_file)
        self.asset_mechanism_engine = AssetMechanismEngine(
            exposure_rules_file,
            direction_rules_file,
            issuer_metadata_file,
            macro_exposure_rules_file,
        )
        self.event_timing_engine = EventTimingEngine(event_timing_rules_file)
        self.market_response_engine = MarketResponseEngine(market_response_rules_file)
        self.exhaustion_engine = ExhaustionEngine()
        self.signal_engine = SignalEngine()

    def acquire(self, feeds: list[dict], since: datetime | None = None) -> list[RawObservation]:
        """Acquire every enabled feed, isolating one provider's failure from the rest.

        A single unreachable/malformed/timed-out feed must never prevent the
        other configured feeds from being acquired. Failures are recorded on
        `self.last_acquisition_status` (provider_id -> ProviderAcquisitionStatus)
        for health/diagnostics reporting; they are never turned into
        fabricated observations.
        """
        observations: list[RawObservation] = []
        status: dict[str, ProviderAcquisitionStatus] = {}
        for feed in feeds:
            if not feed.get("enabled") or feed.get("mode") == "catalogue":
                continue
            provider_id = str(feed.get("provider_id", feed.get("url", "unknown")))
            attempted_at = datetime.now(timezone.utc)
            try:
                fetched = self.acquirer.fetch(
                    provider_id=feed["provider_id"],
                    publisher=feed["publisher"],
                    url=feed["url"],
                    source_tier=int(feed["tier"]),
                    since=since,
                )
            except Exception as exc:  # noqa: BLE001 - one bad provider must not kill acquisition
                status[provider_id] = ProviderAcquisitionStatus(
                    provider_id=provider_id,
                    publisher=str(feed.get("publisher", "")),
                    attempted_at=attempted_at,
                    success=False,
                    observation_count=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            observations.extend(fetched)
            status[provider_id] = ProviderAcquisitionStatus(
                provider_id=provider_id,
                publisher=str(feed.get("publisher", "")),
                attempted_at=attempted_at,
                success=True,
                observation_count=len(fetched),
                error=None,
            )
        self.last_acquisition_status = status
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

    def build_prioritization(self, intelligence: list[StoryIntelligence]) -> list[StoryIntelligence]:
        result: list[StoryIntelligence] = []
        for item in intelligence:
            priorities = self.priority_engine.assess_many(
                item.semantic_events,
                item.evidence,
                item.transmissions,
            )
            result.append(replace(item, priorities=priorities))
        return result

    def build_asset_mechanism(self, intelligence: list[StoryIntelligence]) -> list[StoryIntelligence]:
        """CP8: attach the EVENT -> ASSET -> MECHANISM mapping(s) for each event."""
        result: list[StoryIntelligence] = []
        for item in intelligence:
            mappings = self.asset_mechanism_engine.assess_many(item.semantic_events)
            result.append(replace(item, asset_mechanisms=mappings))
        return result

    def build_event_timing(
        self, intelligence: list[StoryIntelligence], *, as_of: datetime | None = None
    ) -> list[StoryIntelligence]:
        """CP9: attach the pure timing/novelty freshness state for each event."""
        result: list[StoryIntelligence] = []
        for item in intelligence:
            timings = self.event_timing_engine.assess_many(item.semantic_events, as_of=as_of)
            result.append(replace(item, event_timings=timings))
        return result

    def build_market_response(
        self,
        intelligence: list[StoryIntelligence],
        market_observations: Iterable[MarketObservation],
        *,
        as_of: datetime | None = None,
    ) -> list[StoryIntelligence]:
        """CP10: stage the live/synchronized market response for each event's resolved asset.

        Never fabricates an observation: an event whose asset is unresolved,
        or for which no observation exists, receives `None` here rather than
        a guessed response.
        """
        observations = tuple(market_observations)
        result: list[StoryIntelligence] = []
        for item in intelligence:
            asset_mechanisms = item.asset_mechanisms or self.asset_mechanism_engine.assess_many(item.semantic_events)
            responses: list[MarketResponseAssessment | None] = []
            for event, mappings in zip(item.semantic_events, asset_mechanisms):
                primary = AssetMechanismEngine.primary(mappings)
                if primary is None or not primary.resolved or not primary.asset:
                    responses.append(None)
                    continue
                responses.append(
                    self.market_response_engine.assess(
                        event_id=event.event_id,
                        asset=primary.asset,
                        event_time=event.event_time,
                        expected_direction=primary.expected_direction,
                        observations=observations,
                        as_of=as_of,
                    )
                )
            result.append(replace(item, asset_mechanisms=asset_mechanisms, market_responses=tuple(responses)))
        return result

    def build_exhaustion(self, intelligence: list[StoryIntelligence]) -> list[StoryIntelligence]:
        """CP9+CP10 composite: is the event-driven move still early, or already spent?"""
        result: list[StoryIntelligence] = []
        for item in intelligence:
            timings = item.event_timings or self.event_timing_engine.assess_many(item.semantic_events)
            responses = item.market_responses or tuple(None for _ in item.semantic_events)
            exhaustions = tuple(
                self.exhaustion_engine.assess(timing, response)
                for timing, response in zip(timings, responses, strict=True)
            )
            result.append(replace(item, event_timings=timings, exhaustions=exhaustions))
        return result

    def build_signals(
        self,
        intelligence: list[StoryIntelligence],
        *,
        as_of: datetime | None = None,
        market_session_state: str = "UNKNOWN",
    ) -> list[StoryIntelligence]:
        """CP11: combine event/asset/mechanism/timing/market-response/exhaustion into a signal.

        `market_session_state` is a single diagnostic fact for the whole run
        (MARKET_OPEN/MARKET_CLOSED/MARKET_DATA_UNAVAILABLE/UNKNOWN) -- it is
        recorded on every signal for observability and never changes the
        signal-state decision rules.
        """
        result: list[StoryIntelligence] = []
        for item in intelligence:
            asset_mechanisms = item.asset_mechanisms or self.asset_mechanism_engine.assess_many(
                item.semantic_events
            )
            timings = item.event_timings or self.event_timing_engine.assess_many(
                item.semantic_events, as_of=as_of
            )
            responses = item.market_responses or tuple(None for _ in item.semantic_events)
            exhaustions = item.exhaustions or tuple(
                self.exhaustion_engine.assess(timing, response)
                for timing, response in zip(timings, responses, strict=True)
            )
            confirmations = item.market_confirmations or tuple(None for _ in item.semantic_events)
            zipped = zip(
                item.semantic_events, asset_mechanisms, timings, responses, exhaustions, confirmations,
                strict=True,
            )
            signals = [
                self.signal_engine.assess(
                    event,
                    story_id=item.story.cluster_id,
                    asset_mapping=AssetMechanismEngine.primary(mappings),
                    timing=timing,
                    response=response,
                    exhaustion=exhaustion,
                    confirmation=confirmation,
                    as_of=as_of,
                    market_session_state=market_session_state,
                )
                for event, mappings, timing, response, exhaustion, confirmation in zipped
            ]
            result.append(
                replace(
                    item,
                    asset_mechanisms=asset_mechanisms,
                    event_timings=timings,
                    market_responses=responses,
                    exhaustions=exhaustions,
                    signals=tuple(signals),
                )
            )
        return result

    def rank_prioritization(self, intelligence: list[StoryIntelligence]) -> list[PriorityAssessment]:
        assessments = [
            priority
            for item in intelligence
            for priority in item.priorities
        ]
        return self.priority_engine.rank(assessments)

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
