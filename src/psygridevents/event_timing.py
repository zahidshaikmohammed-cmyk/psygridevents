from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum

from .semantic import SemanticEvent


class EventTimingState(StrEnum):
    NEW = "NEW"
    EARLY = "EARLY"
    DEVELOPING = "DEVELOPING"
    LATE = "LATE"
    EXHAUSTED = "EXHAUSTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class EventTimingAssessment:
    event_id: str
    state: EventTimingState
    detection_timestamp: datetime | None
    publication_timestamp: datetime | None
    event_age_seconds: float | None
    since_first_detection_seconds: float | None
    recurrence: str
    repricing_possible: bool
    reason: str


class EventTimingEngine:
    """Classify event freshness using only timestamps explicitly present in data."""

    def __init__(self, *, early_minutes: int = 5, developing_minutes: int = 20, late_minutes: int = 60, exhausted_minutes: int = 180) -> None:
        self.early_seconds = early_minutes * 60
        self.developing_seconds = developing_minutes * 60
        self.late_seconds = late_minutes * 60
        self.exhausted_seconds = exhausted_minutes * 60

    def assess(
        self,
        event: SemanticEvent,
        *,
        as_of: datetime | None,
        first_detection: datetime | None = None,
        repeated: bool = False,
        already_repriced: bool = False,
    ) -> EventTimingAssessment:
        publication = self._aware(event.event_time)
        detection = self._aware(first_detection)
        now = self._aware(as_of)
        if publication is None or now is None:
            return EventTimingAssessment(
                event_id=event.event_id,
                state=EventTimingState.UNKNOWN,
                detection_timestamp=detection,
                publication_timestamp=publication,
                event_age_seconds=None,
                since_first_detection_seconds=None,
                recurrence="repeated" if repeated else "new",
                repricing_possible=already_repriced,
                reason="Publication and evaluation timestamps are both required; no timestamp is fabricated.",
            )

        age = max(0.0, (now - publication).total_seconds())
        since_detection = None if detection is None else max(0.0, (now - detection).total_seconds())
        recurrence = "repeated" if repeated else "new"
        if already_repriced:
            state = EventTimingState.EXHAUSTED
            reason = "Material repricing was established before this signal evaluation."
        elif age <= self.early_seconds:
            state = EventTimingState.NEW
            reason = "Event is inside the initial response window."
        elif age <= self.developing_seconds:
            state = EventTimingState.EARLY
            reason = "Event remains inside the early-development window."
        elif age <= self.late_seconds:
            state = EventTimingState.DEVELOPING
            reason = "Event is developing beyond the initial early window."
        elif age <= self.exhausted_seconds:
            state = EventTimingState.LATE
            reason = "Event is late relative to the configured freshness windows."
        else:
            state = EventTimingState.EXHAUSTED
            reason = "Event age exceeds the configured freshness horizon."

        return EventTimingAssessment(
            event_id=event.event_id,
            state=state,
            detection_timestamp=detection,
            publication_timestamp=publication,
            event_age_seconds=round(age, 3),
            since_first_detection_seconds=round(since_detection, 3) if since_detection is not None else None,
            recurrence=recurrence,
            repricing_possible=already_repriced,
            reason=reason,
        )

    @staticmethod
    def _aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return None
        return value.astimezone(timezone.utc)
