from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from .acquisition import RawObservation
from .normalize import canonical_text

if TYPE_CHECKING:
    from .story_engine import StoryIntelligence


@dataclass(frozen=True)
class EvidenceSpan:
    observation_url: str
    publisher: str
    text: str
    source_tier: int


@dataclass(frozen=True)
class Magnitude:
    value: str
    unit: str
    normalized_value: float | None
    normalized_unit: str | None
    text: str


@dataclass(frozen=True)
class SemanticEvent:
    event_id: str
    story_id: str
    event_type: str
    trigger: str
    event_time: datetime | None
    instruments: tuple[str, ...]
    participants: tuple[str, ...]
    magnitude: Magnitude | None
    direct_effect: str | None
    indirect_effect: str | None
    competitor_effect: str | None
    supply_chain_effect: str | None
    time_horizon: str | None
    novelty_status: str
    surprise_status: str
    modality: str
    negated: bool
    extraction_confidence: float
    evidence: tuple[EvidenceSpan, ...]
    uncertainty: tuple[str, ...]
    market_mechanism: str | None
    novelty_score: float = 0.0
    novelty_reason: str | None = None


class SemanticExtractor:
    """Conservative, auditable event extraction from clustered financial stories."""

    def __init__(self, rules_file: str | Path) -> None:
        data = yaml.safe_load(Path(rules_file).read_text(encoding="utf-8")) or {}
        self.rules: dict[str, Any] = data.get("event_rules", {})
        self.negation_patterns = tuple(data.get("negation_patterns", []))
        self.modality_patterns = data.get("modality_patterns", {})
        self.horizon_patterns = data.get("horizon_patterns", {})
        self.effect_patterns = data.get("effect_patterns", {})
        self.magnitude_pattern = re.compile(data.get("magnitude_regex", r"$^"), re.I)

    def extract(self, intelligence: StoryIntelligence) -> list[SemanticEvent]:
        representative = intelligence.story.representative
        text = self._document_text(representative)
        events: list[SemanticEvent] = []
        for event_type, trigger, score in self._candidate_types(text):
            events.append(
                self._build_event(
                    intelligence,
                    representative,
                    event_type=event_type,
                    trigger=trigger,
                    trigger_score=score,
                )
            )
        return events

    def _build_event(
        self,
        intelligence: StoryIntelligence,
        observation: RawObservation,
        *,
        event_type: str,
        trigger: str,
        trigger_score: float,
    ) -> SemanticEvent:
        text = self._document_text(observation)
        trigger_position = text.lower().find(trigger.lower())
        sentence = self._sentence_around(text, trigger_position)
        negated = any(re.search(pattern, sentence, re.I) for pattern in self.negation_patterns)
        modality = self._modality(sentence)
        if negated and modality == "asserted":
            modality = "negated"

        matched_entities = tuple(sorted({match.instrument for match in intelligence.entities}))
        participants = tuple(sorted({match.alias for match in intelligence.entities}))
        magnitude = self._extract_magnitude(sentence)
        effects = self._extract_effects(sentence)
        horizon = self._extract_horizon(sentence)

        uncertainty: list[str] = []
        if not matched_entities:
            uncertainty.append("No configured instrument was explicitly resolved from this story.")
        if not magnitude:
            uncertainty.append("No quantified magnitude was extracted from the event sentence.")
        if modality != "asserted":
            uncertainty.append(f"Source language is marked as {modality}.")
        uncertainty.append("Event timestamp is source publication time unless an explicit event date is extracted later.")

        confidence = min(0.99, max(0.20, trigger_score))
        if not matched_entities:
            confidence -= 0.10
        if negated:
            confidence -= 0.25

        story_id = self._story_id(intelligence)
        event_id = self._event_id(
            story_id=story_id,
            event_type=event_type,
            instruments=matched_entities,
        )
        evidence = tuple(
            EvidenceSpan(
                observation_url=item.url,
                publisher=item.publisher,
                text=self._document_text(item)[:1000],
                source_tier=item.source_tier,
            )
            for item in intelligence.story.observations
        )
        return SemanticEvent(
            event_id=event_id,
            story_id=story_id,
            event_type=event_type,
            trigger=trigger,
            event_time=observation.published_at,
            instruments=matched_entities,
            participants=participants,
            magnitude=magnitude,
            direct_effect=effects.get("direct"),
            indirect_effect=effects.get("indirect"),
            competitor_effect=effects.get("competitor"),
            supply_chain_effect=effects.get("supply_chain"),
            time_horizon=horizon,
            novelty_status="not_assessed",
            surprise_status="not_assessed",
            modality=modality,
            negated=negated,
            extraction_confidence=round(max(0.0, confidence), 3),
            evidence=evidence,
            uncertainty=tuple(uncertainty),
            market_mechanism=None,
        )

    def _candidate_types(self, text: str) -> list[tuple[str, str, float]]:
        lower = text.lower()
        candidates: list[tuple[str, str, float]] = []
        for event_type, rule in self.rules.items():
            for trigger in rule.get("triggers", []):
                if re.search(rf"(?<!\w){re.escape(trigger.lower())}(?!\w)", lower):
                    candidates.append((event_type, trigger, float(rule.get("base_confidence", 0.60))))
        candidates.sort(key=lambda item: (-item[2], item[0], item[1]))
        return candidates[:3]

    @staticmethod
    def _document_text(observation: RawObservation) -> str:
        return " ".join(part for part in (observation.title, observation.summary) if part).strip()

    @staticmethod
    def _sentence_around(text: str, position: int) -> str:
        if position < 0:
            return text[:500]
        starts = [text.rfind(mark, 0, position) for mark in ".!?\n"]
        start = max(starts) + 1
        ends = [idx for mark in ".!?\n" if (idx := text.find(mark, position)) >= 0]
        end = min(ends) if ends else len(text)
        return text[start:end].strip()[:1000]

    def _extract_magnitude(self, sentence: str) -> Magnitude | None:
        match = self.magnitude_pattern.search(sentence)
        if not match:
            return None
        text = match.group(0).strip()
        number_match = re.search(r"[-+]?\d+(?:\.\d+)?", text.replace(",", ""))
        normalized_value = float(number_match.group(0)) if number_match else None
        unit = re.sub(r"[-+\d.,\s]", "", text)
        return Magnitude(
            text=text,
            value=text,
            unit=unit,
            normalized_value=normalized_value,
            normalized_unit=unit.lower() or None,
        )

    def _modality(self, sentence: str) -> str:
        for modality, patterns in self.modality_patterns.items():
            if any(re.search(pattern, sentence, re.I) for pattern in patterns):
                return modality
        return "asserted"

    def _extract_horizon(self, sentence: str) -> str | None:
        for horizon, patterns in self.horizon_patterns.items():
            if any(re.search(pattern, sentence, re.I) for pattern in patterns):
                return horizon
        return None

    def _extract_effects(self, sentence: str) -> dict[str, str]:
        effects: dict[str, str] = {}
        for effect_name, patterns in self.effect_patterns.items():
            for pattern in patterns:
                match = re.search(pattern, sentence, re.I)
                if match:
                    effects[effect_name] = match.group(0).strip()
                    break
        return effects

    @staticmethod
    def _story_id(intelligence: StoryIntelligence) -> str:
        text = "|".join(item.url for item in intelligence.story.observations)
        return "story-" + hashlib.sha256(canonical_text(text).encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _event_id(*, story_id: str, event_type: str, instruments: tuple[str, ...]) -> str:
        key = "|".join((story_id, event_type, ",".join(instruments)))
        return "event-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
