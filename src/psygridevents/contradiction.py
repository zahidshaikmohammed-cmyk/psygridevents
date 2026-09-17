from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from .semantic import SemanticEvent


@dataclass(frozen=True)
class ContradictionAssessment:
    event_id: str
    status: str
    narrative_state: str
    matched_event_id: str | None
    similarity: float
    reason: str


class ContradictionEngine:
    """Detect evidence-level conflicts between related semantic events.

    This engine compares structured events only. It does not infer a conflict
    merely because two headlines use different wording, and it never treats
    market price action as a contradiction; that belongs to market confirmation.
    """

    def __init__(self, rules_file: str | Path) -> None:
        data = yaml.safe_load(Path(rules_file).read_text(encoding="utf-8")) or {}
        self.min_similarity = float(data.get("min_similarity", 0.50))
        self.related_similarity = float(data.get("related_similarity", 0.35))
        self.positive_patterns = tuple(data.get("positive_patterns", []))
        self.negative_patterns = tuple(data.get("negative_patterns", []))

    @staticmethod
    def _tokens(value: str | None) -> set[str]:
        if not value:
            return set()
        return {token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) > 2}

    def _fingerprint(self, event: SemanticEvent) -> set[str]:
        values = [
            event.trigger,
            event.direct_effect,
            event.indirect_effect,
            event.competitor_effect,
            event.supply_chain_effect,
        ]
        result: set[str] = set()
        for value in values:
            result |= self._tokens(value)
        return result

    def _similarity(self, current: SemanticEvent, previous: SemanticEvent) -> float:
        if current.event_type != previous.event_type:
            return 0.0
        current_instruments = set(current.instruments)
        previous_instruments = set(previous.instruments)
        instrument_match = bool(current_instruments & previous_instruments)
        participant_match = bool(set(current.participants) & set(previous.participants))
        if not instrument_match and not participant_match:
            return 0.0
        left = self._fingerprint(current)
        right = self._fingerprint(previous)
        lexical = len(left & right) / len(left | right) if left and right else 0.0
        anchor = 1.0 if instrument_match else 0.65
        return round(0.65 * anchor + 0.35 * lexical, 3)

    def _polarity(self, event: SemanticEvent) -> str:
        text = " ".join(
            value or ""
            for value in (
                event.trigger,
                event.direct_effect,
                event.indirect_effect,
                event.competitor_effect,
                event.supply_chain_effect,
            )
        ).lower()
        positive = any(re.search(pattern, text, re.IGNORECASE) for pattern in self.positive_patterns)
        negative = any(re.search(pattern, text, re.IGNORECASE) for pattern in self.negative_patterns)
        if positive and not negative:
            return "positive"
        if negative and not positive:
            return "negative"
        return "neutral"

    def assess(
        self,
        event: SemanticEvent,
        history: Iterable[SemanticEvent],
    ) -> ContradictionAssessment:
        if event.negated or event.modality != "asserted":
            return ContradictionAssessment(
                event_id=event.event_id,
                status="not_assessed",
                narrative_state="unresolved",
                matched_event_id=None,
                similarity=0.0,
                reason="Non-asserted or negated event is excluded from contradiction assessment.",
            )

        best: tuple[float, SemanticEvent] | None = None
        for previous in history:
            if previous.event_id == event.event_id:
                continue
            similarity = self._similarity(event, previous)
            if best is None or similarity > best[0]:
                best = (similarity, previous)

        if best is None or best[0] < self.related_similarity:
            return ContradictionAssessment(
                event_id=event.event_id,
                status="no_comparison",
                narrative_state="new",
                matched_event_id=None,
                similarity=best[0] if best else 0.0,
                reason="No sufficiently related historical event was found.",
            )

        similarity, previous = best
        if similarity < self.min_similarity:
            return ContradictionAssessment(
                event_id=event.event_id,
                status="related",
                narrative_state="unresolved",
                matched_event_id=previous.event_id,
                similarity=similarity,
                reason="A related event exists, but the relationship is not strong enough to classify.",
            )

        current_polarity = self._polarity(event)
        previous_polarity = self._polarity(previous)
        if {current_polarity, previous_polarity} == {"positive", "negative"}:
            return ContradictionAssessment(
                event_id=event.event_id,
                status="contradicted",
                narrative_state="conflicted",
                matched_event_id=previous.event_id,
                similarity=similarity,
                reason="Related events describe opposing documented effect polarity.",
            )

        if current_polarity == previous_polarity and current_polarity != "neutral":
            return ContradictionAssessment(
                event_id=event.event_id,
                status="corroborated",
                narrative_state="supported",
                matched_event_id=previous.event_id,
                similarity=similarity,
                reason="Related events describe the same documented effect polarity.",
            )

        return ContradictionAssessment(
            event_id=event.event_id,
            status="related",
            narrative_state="updated",
            matched_event_id=previous.event_id,
            similarity=similarity,
            reason="Related event found, but no opposing polarity is established.",
        )

    def assess_many(
        self,
        events: list[SemanticEvent] | tuple[SemanticEvent, ...],
        history: Iterable[SemanticEvent],
    ) -> tuple[ContradictionAssessment, ...]:
        return tuple(self.assess(event, history) for event in events)
