from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .semantic import SemanticEvent

STATES = ("new", "early", "developing", "late", "exhausted", "unknown")

# Novelty states that mean "this is not fresh information even if the wording
# is new", per NoveltyEngine. A repeated/repackaged event never gets treated
# as a fresh early opportunity purely because another source repeated it.
_REPEAT_NOVELTY = {"repeat"}
_STALE_NOVELTY = {"stale_repackaged"}


@dataclass(frozen=True)
class EventTimingAssessment:
    """CP9: how fresh is this event, independent of market price behavior.

    This intentionally does not look at price/volume; that belongs to CP10.
    It combines event age (event_time vs as_of) with the existing novelty
    classification so a repeated or repackaged story cannot manufacture a
    fresh early window merely because another publisher repeated it.
    """

    event_id: str
    event_time: datetime | None
    as_of: datetime
    age_hours: float | None
    state: str
    novelty_status: str
    is_repackaged: bool
    uncertainty: tuple[str, ...]
    reason: str


class EventTimingEngine:
    def __init__(self, rules_file: str | Path) -> None:
        data = yaml.safe_load(Path(rules_file).read_text(encoding="utf-8")) or {}
        self.new_within = float(data.get("new_within_minutes", 15)) / 60.0
        self.early_within = float(data.get("early_within_hours", 4))
        self.developing_within = float(data.get("developing_within_hours", 24))
        self.late_within = float(data.get("late_within_hours", 72))

    def assess(self, event: SemanticEvent, *, as_of: datetime | None = None) -> EventTimingAssessment:
        current = as_of or datetime.now(timezone.utc)

        if event.negated or event.modality != "asserted":
            return EventTimingAssessment(
                event_id=event.event_id,
                event_time=event.event_time,
                as_of=current,
                age_hours=None,
                state="unknown",
                novelty_status=event.novelty_status,
                is_repackaged=False,
                uncertainty=(f"Event language is {event.modality}; timing state is not assessed.",),
                reason="Non-asserted or negated events do not receive a timing state.",
            )

        if event.event_time is None:
            return EventTimingAssessment(
                event_id=event.event_id,
                event_time=None,
                as_of=current,
                age_hours=None,
                state="unknown",
                novelty_status=event.novelty_status,
                is_repackaged=event.novelty_status in (_REPEAT_NOVELTY | _STALE_NOVELTY),
                uncertainty=("Event timestamp is unavailable; timing state cannot be established.",),
                reason="No event or publication timestamp is available.",
            )

        age_hours = self._age_hours(current, event.event_time)
        uncertainty: list[str] = [
            "Event age is measured from source publication time unless an explicit "
            "event-occurrence time was extracted; the two may differ."
        ]

        if event.novelty_status in _STALE_NOVELTY:
            return EventTimingAssessment(
                event_id=event.event_id,
                event_time=event.event_time,
                as_of=current,
                age_hours=round(age_hours, 2),
                state="exhausted",
                novelty_status=event.novelty_status,
                is_repackaged=True,
                uncertainty=tuple(uncertainty),
                reason="Event closely matches an older event and is stale/repackaged coverage; "
                "it cannot be treated as a fresh early opportunity.",
            )

        if event.novelty_status in _REPEAT_NOVELTY:
            return EventTimingAssessment(
                event_id=event.event_id,
                event_time=event.event_time,
                as_of=current,
                age_hours=round(age_hours, 2),
                state="late",
                novelty_status=event.novelty_status,
                is_repackaged=True,
                uncertainty=tuple(uncertainty),
                reason="Event is materially the same as a recently accepted event; a repeated "
                "headline does not restart the early window.",
            )

        state = self._age_state(age_hours)
        return EventTimingAssessment(
            event_id=event.event_id,
            event_time=event.event_time,
            as_of=current,
            age_hours=round(age_hours, 2),
            state=state,
            novelty_status=event.novelty_status,
            is_repackaged=False,
            uncertainty=tuple(uncertainty),
            reason=f"Event age is {round(age_hours, 2)} hour(s); classified as {state} by configured thresholds.",
        )

    def assess_many(
        self, events: list[SemanticEvent] | tuple[SemanticEvent, ...], *, as_of: datetime | None = None
    ) -> tuple[EventTimingAssessment, ...]:
        return tuple(self.assess(event, as_of=as_of) for event in events)

    def _age_state(self, age_hours: float) -> str:
        if age_hours <= self.new_within:
            return "new"
        if age_hours <= self.early_within:
            return "early"
        if age_hours <= self.developing_within:
            return "developing"
        if age_hours <= self.late_within:
            return "late"
        return "exhausted"

    @staticmethod
    def _age_hours(as_of: datetime, event_time: datetime) -> float:
        left = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        right = event_time if event_time.tzinfo else event_time.replace(tzinfo=timezone.utc)
        return max(0.0, (left - right).total_seconds() / 3600.0)
