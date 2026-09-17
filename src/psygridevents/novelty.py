from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

from .semantic import SemanticEvent


@dataclass(frozen=True)
class NoveltyAssessment:
    status: str
    score: float
    similarity: float
    matched_event_id: str | None
    age_hours: float | None
    reason: str


class NoveltyEngine:
    """Determine whether a semantic event is new, repeated, updated, or stale.

    Novelty is evaluated against previously accepted semantic events. It is deliberately
    conservative: a textually similar event is not treated as new merely because it was
    published by another outlet. Scores are heuristic evidence scores, not probabilities.
    """

    STATUSES = {"new", "repeat", "follow_up", "updated", "stale_repackaged", "unknown"}

    def __init__(
        self,
        *,
        lookback_hours: int = 168,
        repeat_similarity: float = 0.86,
        related_similarity: float = 0.62,
        stale_after_hours: int = 24,
    ) -> None:
        self.lookback = timedelta(hours=lookback_hours)
        self.repeat_similarity = repeat_similarity
        self.related_similarity = related_similarity
        self.stale_after = timedelta(hours=stale_after_hours)

    def assess(
        self,
        event: SemanticEvent,
        history: Iterable[SemanticEvent],
        *,
        as_of: datetime | None = None,
    ) -> NoveltyAssessment:
        current_time = as_of or event.event_time or datetime.now(timezone.utc)
        candidates = [
            previous
            for previous in history
            if previous.event_id != event.event_id
            and self._compatible(previous, event)
            and self._age(current_time, previous.event_time) is not None
            and self._age(current_time, previous.event_time) <= self.lookback
        ]

        if not candidates:
            return NoveltyAssessment(
                status="new",
                score=1.0,
                similarity=0.0,
                matched_event_id=None,
                age_hours=None,
                reason="No compatible prior event was found inside the novelty lookback window.",
            )

        ranked = sorted(
            ((self._similarity(event, previous), previous) for previous in candidates),
            key=lambda item: item[0],
            reverse=True,
        )
        similarity, previous = ranked[0]
        age_hours = self._age(current_time, previous.event_time)
        assert age_hours is not None

        if similarity >= self.repeat_similarity:
            if timedelta(hours=age_hours) <= self.stale_after:
                status = "repeat"
                score = 0.05
                reason = "The event is materially the same as a recent accepted event."
            else:
                status = "stale_repackaged"
                score = 0.20
                reason = "The event closely matches an older event and appears to be repackaged coverage."
        elif similarity >= self.related_similarity:
            if self._material_update(event, previous):
                status = "updated"
                score = 0.65
                reason = "The event matches a prior event family but contains materially new event detail."
            else:
                status = "follow_up"
                score = 0.40
                reason = "The event appears to continue an existing event family without a clear material update."
        else:
            status = "new"
            score = 0.90
            reason = "A compatible prior event exists, but semantic similarity is below the related-event threshold."

        return NoveltyAssessment(
            status=status,
            score=score,
            similarity=round(similarity, 3),
            matched_event_id=previous.event_id,
            age_hours=round(age_hours, 2),
            reason=reason,
        )

    def assess_many(
        self,
        events: Iterable[SemanticEvent],
        history: Iterable[SemanticEvent],
        *,
        as_of: datetime | None = None,
    ) -> dict[str, NoveltyAssessment]:
        history_tuple = tuple(history)
        return {
            event.event_id: self.assess(event, history_tuple, as_of=as_of)
            for event in events
        }

    @staticmethod
    def _compatible(left: SemanticEvent, right: SemanticEvent) -> bool:
        if left.event_type != right.event_type:
            return False
        if left.instruments and right.instruments:
            return bool(set(left.instruments) & set(right.instruments))
        left_participants = {item.lower() for item in left.participants}
        right_participants = {item.lower() for item in right.participants}
        return bool(left_participants & right_participants) or not left_participants or not right_participants

    @classmethod
    def _similarity(cls, left: SemanticEvent, right: SemanticEvent) -> float:
        left_tokens = cls._tokens(left)
        right_tokens = cls._tokens(right)
        lexical = cls._jaccard(left_tokens, right_tokens)

        trigger_left = set(cls._tokenize(left.trigger))
        trigger_right = set(cls._tokenize(right.trigger))
        trigger_similarity = cls._jaccard(trigger_left, trigger_right)

        magnitude_similarity = 1.0 if cls._same_magnitude(left, right) else 0.0
        return (0.55 * lexical) + (0.25 * trigger_similarity) + (0.20 * magnitude_similarity)

    @classmethod
    def _tokens(cls, event: SemanticEvent) -> set[str]:
        parts = [event.trigger]
        parts.extend(event.participants)
        parts.extend(event.instruments)
        parts.extend(
            item.text
            for evidence in event.evidence[:3]
            for item in [evidence]
        )
        if event.magnitude:
            parts.append(event.magnitude.text)
        return set(cls._tokenize(" ".join(parts)))

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        return [token for token in re.findall(r"[a-z0-9]+", text.lower()) if len(token) > 2]

    @staticmethod
    def _jaccard(left: set[str], right: set[str]) -> float:
        if not left and not right:
            return 1.0
        union = left | right
        return len(left & right) / len(union) if union else 0.0

    @staticmethod
    def _same_magnitude(left: SemanticEvent, right: SemanticEvent) -> bool:
        if left.magnitude is None or right.magnitude is None:
            return left.magnitude is right.magnitude
        if left.magnitude.normalized_unit != right.magnitude.normalized_unit:
            return False
        if left.magnitude.normalized_value is None or right.magnitude.normalized_value is None:
            return left.magnitude.text.lower() == right.magnitude.text.lower()
        base = max(abs(left.magnitude.normalized_value), abs(right.magnitude.normalized_value), 1.0)
        return abs(left.magnitude.normalized_value - right.magnitude.normalized_value) / base <= 0.05

    @classmethod
    def _material_update(cls, current: SemanticEvent, previous: SemanticEvent) -> bool:
        if current.magnitude is not None and previous.magnitude is not None:
            if not cls._same_magnitude(current, previous):
                return True
        current_effects = {
            current.direct_effect,
            current.indirect_effect,
            current.competitor_effect,
            current.supply_chain_effect,
            current.time_horizon,
        }
        previous_effects = {
            previous.direct_effect,
            previous.indirect_effect,
            previous.competitor_effect,
            previous.supply_chain_effect,
            previous.time_horizon,
        }
        return current_effects != previous_effects

    @staticmethod
    def _age(as_of: datetime, event_time: datetime | None) -> float | None:
        if event_time is None:
            return None
        left = as_of
        right = event_time
        if left.tzinfo is None:
            left = left.replace(tzinfo=timezone.utc)
        if right.tzinfo is None:
            right = right.replace(tzinfo=timezone.utc)
        delta = left - right
        return max(0.0, delta.total_seconds() / 3600.0)
