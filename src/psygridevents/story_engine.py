from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable
import json

import yaml

from .acquisition import RSSAcquirer, RawObservation
from .clustering import StoryCluster, cluster_stories
from .contradiction import ContradictionAssessment, ContradictionEngine
from .deduplication import deduplicate
from .entity_resolution import InstrumentResolver, EntityMatch
from .evidence import EvidenceAssessment, assess_evidence
from .event_mapping import EventMechanismAssessment, EventMechanismEngine
from .issuer_runtime import IssuerMasterRuntime
from .market_confirmation import MarketConfirmationAssessment, MarketConfirmationEngine, MarketObservation
from .materiality import MaterialityEngine
from .normalize import normalized_observation
from .novelty import NoveltyEngine
from .priority import PriorityAssessment, PriorityEngine
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
    priorities: tuple[PriorityAssessment, ...] = ()
    event_mappings: tuple[EventMechanismAssessment, ...] = ()


class StoryEngine:
    """Acquisition-to-priority pipeline with explicit fail-closed CP stages."""

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
        event_mapping_rules_file: str | Path | None = None,
        issuer_runtime_config_file: str | Path | None = None,
    ) -> None:
        self.acquirer = RSSAcquirer()
        self.resolver = InstrumentResolver.from_instrument_file(instrument_file)
        config_dir = Path(instrument_file).parent
        if issuer_runtime_config_file:
            self.resolver = self._resolver_with_runtime_issuer_master(
                self.resolver,
                instrument_file,
                issuer_runtime_config_file,
            )
        self.semantic_extractor = SemanticExtractor(semantic_rules_file or config_dir / "semantic_rules.yaml")
        self.novelty_engine = NoveltyEngine()
        self.materiality_engine = MaterialityEngine(materiality_rules_file or config_dir / "materiality_rules.yaml")
        self.transmission_engine = TransmissionEngine(exposure_rules_file or config_dir / "exposure_rules.yaml", issuer_metadata_file)
        self.contradiction_engine = ContradictionEngine(contradiction_rules_file or config_dir / "contradiction_rules.yaml")
        self.market_confirmation_engine = MarketConfirmationEngine(market_confirmation_rules_file or config_dir / "market_confirmation_rules.yaml")
        self.priority_engine = PriorityEngine(priority_rules_file or config_dir / "priority_rules.yaml")
        self.event_mechanism_engine = EventMechanismEngine(event_mapping_rules_file or config_dir / "event_mapping_rules.yaml")

    def acquire(self, feeds: list[dict], since: datetime | None = None) -> list[RawObservation]:
        observations: list[RawObservation] = []
        for feed in feeds:
            if not feed.get("enabled") or feed.get("mode") == "catalogue":
                continue
            observations.extend(self.acquirer.fetch(provider_id=feed["provider_id"], publisher=feed["publisher"], url=feed["url"], source_tier=int(feed["tier"]), since=since))
        return [normalized_observation(item) for item in observations]

    def build_stories(self, observations: list[RawObservation], historical_events: tuple[SemanticEvent, ...] = (), *, as_of: datetime | None = None) -> list[StoryIntelligence]:
        decisions = deduplicate(observations)
        unique = [decision.observation for decision in decisions if decision.duplicate_of is None]
        intelligence = [self._intelligence(story) for story in cluster_stories(unique)]
        return self.build_novelty_and_contradiction(intelligence, historical_events, as_of=as_of)

    def build_novelty_and_contradiction(self, intelligence: list[StoryIntelligence], historical_events: tuple[SemanticEvent, ...], *, as_of: datetime | None = None) -> list[StoryIntelligence]:
        if not historical_events:
            return intelligence
        result: list[StoryIntelligence] = []
        for item in intelligence:
            events: list[SemanticEvent] = []
            contradictions: list[ContradictionAssessment] = []
            for event in item.semantic_events:
                novelty = self.novelty_engine.assess(event, historical_events, as_of=as_of)
                contradiction = self.contradiction_engine.assess(event, historical_events)
                contradictions.append(contradiction)
                events.append(replace(event, novelty_status=novelty.status, novelty_score=novelty.score, novelty_reason=novelty.reason, contradiction_status=contradiction.status, narrative_state=contradiction.narrative_state, contradiction_score=contradiction.similarity, contradiction_reason=contradiction.reason))
            result.append(replace(item, semantic_events=tuple(events), contradictions=tuple(contradictions)))
        return result

    def build_materiality(self, intelligence: list[StoryIntelligence]) -> list[StoryIntelligence]:
        result = []
        for item in intelligence:
            events = []
            for event in item.semantic_events:
                assessment = self.materiality_engine.assess(event)
                events.append(replace(event, materiality_status=assessment.status, materiality_score=assessment.score, materiality_reason=assessment.reason))
            result.append(replace(item, semantic_events=tuple(events)))
        return result

    def build_transmission(self, intelligence: list[StoryIntelligence]) -> list[StoryIntelligence]:
        return [replace(item, transmissions=self.transmission_engine.assess_many(item.semantic_events)) for item in intelligence]

    def build_event_mapping(self, intelligence: list[StoryIntelligence]) -> list[StoryIntelligence]:
        return [replace(item, event_mappings=self.event_mechanism_engine.assess_many(item.semantic_events)) for item in intelligence]

    def build_contradiction(self, intelligence: list[StoryIntelligence], historical_events: tuple[SemanticEvent, ...]) -> list[StoryIntelligence]:
        return self.build_novelty_and_contradiction(intelligence, historical_events)

    def build_market_confirmation(self, intelligence: list[StoryIntelligence], market_observations: Iterable[MarketObservation]) -> list[StoryIntelligence]:
        observations = tuple(market_observations)
        result = []
        for item in intelligence:
            assessments = self.market_confirmation_engine.assess_many(item.semantic_events, observations)
            events = [replace(event, market_confirmation_status=assessment.status, market_confirmation_score=assessment.score, market_confirmation_reason=assessment.reason) for event, assessment in zip(item.semantic_events, assessments)]
            result.append(replace(item, semantic_events=tuple(events), market_confirmations=assessments))
        return result

    def build_prioritization(self, intelligence: list[StoryIntelligence]) -> list[StoryIntelligence]:
        return [replace(item, priorities=self.priority_engine.assess_many(item.semantic_events, item.evidence, item.transmissions)) for item in intelligence]

    def rank_prioritization(self, intelligence: list[StoryIntelligence]) -> list[PriorityAssessment]:
        return self.priority_engine.rank(priority for item in intelligence for priority in item.priorities)

    def _intelligence(self, story: StoryCluster) -> StoryIntelligence:
        text = " ".join(f"{item.title} {item.summary}" for item in story.observations)
        matches = tuple(self.resolver.resolve(text))
        evidence = assess_evidence(list(story.observations))
        base = StoryIntelligence(story=story, entities=matches, evidence=evidence)
        events = tuple(self.semantic_extractor.extract(base))
        return StoryIntelligence(story=story, entities=matches, evidence=evidence, semantic_events=events)

    @staticmethod
    def _resolver_with_runtime_issuer_master(base: InstrumentResolver, instrument_file: str | Path, runtime_config_file: str | Path) -> InstrumentResolver:
        try:
            config = yaml.safe_load(Path(runtime_config_file).read_text(encoding="utf-8")) or {}
            source = config.get("source", {})
            cache_file = Path(config.get("cache_file", "data/state/issuer_master.json"))
            if not cache_file.is_absolute():
                cache_file = Path(instrument_file).parent.parent / cache_file
            runtime = IssuerMasterRuntime(
                instrument_file,
                cache_file,
                str(source["csv_url"]),
                max_age_hours=float(config.get("max_age_hours", 24)),
                timeout=float(config.get("request_timeout_seconds", 15)),
            )
            return InstrumentResolver.from_issuer_records(runtime.load(), base)
        except (OSError, KeyError, TypeError, ValueError):
            return base

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def load_history(path: str | Path, *, max_events: int = 2000) -> tuple[SemanticEvent, ...]:
        path = Path(path)
        if not path.exists():
            return ()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return tuple(SemanticEvent(**item) for item in payload.get("events", [])[-max_events:])
        except (OSError, ValueError, TypeError, KeyError):
            return ()

    @staticmethod
    def save_history(path: str | Path, events: Iterable[SemanticEvent], *, max_events: int = 2000) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        selected = list(events)[-max_events:]
        path.write_text(json.dumps({"version": "1.0", "events": [StoryEngine._jsonable(event) for event in selected]}, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    def build_event_timing(self, intelligence, *, as_of=None):
        from .event_timing import EventTimingEngine
        engine = EventTimingEngine()
        now = as_of or self.now()
        return [(item, tuple(engine.assess(event, as_of=now) for event in item.semantic_events)) for item in intelligence]

    def build_signals(self, intelligence, market_adapter, *, as_of=None):
        from .signal import EventSignalEngine
        now = as_of or self.now()
        signal_engine = EventSignalEngine()
        output = []
        for item in intelligence:
            timings = self.build_event_timing([item], as_of=now)[0][1]
            symbols = tuple(symbol for mapping in item.event_mappings for symbol in mapping.assets)
            observations = market_adapter.observations(symbols, now, now)
            output.extend(signal_engine.assess_many(item.semantic_events, item.event_mappings, timings, observations, as_of=now))
        return tuple(output)

    @staticmethod
    def _jsonable(value):
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, tuple):
            return [StoryEngine._jsonable(item) for item in value]
        if isinstance(value, list):
            return [StoryEngine._jsonable(item) for item in value]
        if isinstance(value, dict):
            return {str(k): StoryEngine._jsonable(v) for k, v in value.items()}
        if hasattr(value, "__dataclass_fields__"):
            return {name: StoryEngine._jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
        return value
