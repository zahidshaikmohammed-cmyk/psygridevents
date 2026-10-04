"""Stateful signal book: state transitions with dwell/cooldown, alert de-duplication and TOP-N ranking."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from .storage import Store, StoredSignal

NO_SIGNAL = "NO_SIGNAL"
WATCH = "WATCH"
EARLY_LONG = "EARLY_LONG"
EARLY_SHORT = "EARLY_SHORT"
CONFIRMED = "CONFIRMED"
INVALIDATED = "INVALIDATED"
EXHAUSTED = "EXHAUSTED"

SIGNAL_STATES = (NO_SIGNAL, WATCH, EARLY_LONG, EARLY_SHORT, CONFIRMED, INVALIDATED, EXHAUSTED)
ACTIONABLE = {EARLY_LONG, EARLY_SHORT, CONFIRMED}
TERMINAL = {INVALIDATED, EXHAUSTED}
_LEVEL = {NO_SIGNAL: 0, WATCH: 1, EARLY_LONG: 2, EARLY_SHORT: 2, CONFIRMED: 3}


@dataclass(frozen=True)
class Transition:
    signal_key: str
    symbol: str
    from_state: str | None
    to_state: str
    at: datetime
    score: float
    reason: str
    alert: bool


class SignalBook:
    """Owns the per-(event, symbol, direction) signal state; persists every change."""

    def __init__(self, store: Store, *, cooldown_minutes: float, dwell_minutes: float) -> None:
        self.store = store
        self.cooldown = timedelta(minutes=cooldown_minutes)
        self.dwell = timedelta(minutes=dwell_minutes)
        self.records: dict[str, StoredSignal] = {}

    def restore(self, trade_date: str) -> int:
        self.records = {item.signal_key: item for item in self.store.load_signals(active_only=True, trade_date=trade_date)}
        return len(self.records)

    def roll_day(self, trade_date: str) -> int:
        """Deactivate yesterday's signals; they stay in the database for history."""
        count = self.store.deactivate_signals_before(trade_date)
        self.records = {key: value for key, value in self.records.items() if value.trade_date >= trade_date}
        return count

    def get(self, key: str) -> StoredSignal | None:
        return self.records.get(key)

    def _allowed(self, current: str, proposed: str, last_change: datetime, now: datetime) -> tuple[bool, str]:
        if current == proposed:
            return False, "unchanged"
        if current in TERMINAL:
            return False, f"{current} is terminal for this event/symbol/direction"
        if proposed in TERMINAL:
            return True, "terminal"
        if current == CONFIRMED:
            return False, "CONFIRMED is held until invalidated or exhausted"
        if _LEVEL.get(proposed, 0) > _LEVEL.get(current, 0):
            return True, "upgrade"
        if current in (EARLY_LONG, EARLY_SHORT) and proposed in (EARLY_LONG, EARLY_SHORT):
            return False, "direction flip must invalidate (handled by a new signal key)"
        if now - last_change < self.dwell:
            return False, "minimum dwell time not elapsed"
        return True, "downgrade"

    def update(
        self,
        *,
        signal_key: str,
        symbol: str,
        event_id: str,
        story_id: str | None,
        direction: str,
        proposed_state: str,
        score: float,
        reason: str,
        payload: dict[str, Any],
        trade_date: str,
        now: datetime,
    ) -> Transition | None:
        record = self.records.get(signal_key)
        if record is None:
            record = StoredSignal(
                signal_key=signal_key, symbol=symbol, event_id=event_id, story_id=story_id, direction=direction,
                state=NO_SIGNAL, opportunity_score=score, first_seen_at=now, first_actionable_at=None,
                last_state_change_at=now, last_alert_at=None, last_alert_state=None, active=True,
                trade_date=trade_date, payload=payload,
            )
            created = True
        else:
            created = False
        allowed, why = self._allowed(record.state, proposed_state, record.last_state_change_at, now)
        transition: Transition | None = None
        if allowed:
            previous = record.state
            alert = False
            if proposed_state in ACTIONABLE or (proposed_state in TERMINAL and previous in ACTIONABLE):
                same_recent = (
                    record.last_alert_state == proposed_state
                    and record.last_alert_at is not None
                    and now - record.last_alert_at < self.cooldown
                )
                alert = not same_recent
            first_actionable = record.first_actionable_at
            if proposed_state in ACTIONABLE and first_actionable is None:
                first_actionable = now
            record = replace(
                record, state=proposed_state, opportunity_score=score, last_state_change_at=now,
                first_actionable_at=first_actionable,
                last_alert_at=now if alert else record.last_alert_at,
                last_alert_state=proposed_state if alert else record.last_alert_state,
                payload=payload | {"state": proposed_state},
            )
            transition = Transition(signal_key, symbol, None if created else previous, proposed_state, now, score, reason, alert)
            self.store.record_transition(signal_key, symbol, None if created else previous, proposed_state, now, score, reason)
        else:
            record = replace(record, opportunity_score=score, payload=payload | {"state": record.state, "held_reason": why if why != "unchanged" else None})
        self.records[signal_key] = record
        self.store.upsert_signal(record)
        return transition

    def active(self) -> list[StoredSignal]:
        return [record for record in self.records.values() if record.active]


@dataclass
class RankedEntry:
    rank: int
    signal_key: str
    symbol: str
    score: float
    payload: dict[str, Any]


@dataclass
class TopRanker:
    """TOP-N with stability: an incumbent keeps its place unless clearly beaten."""

    top_n: int
    min_score: float
    swap_margin: float
    multiple_event_bonus: float
    previous: list[str] = field(default_factory=list)  # symbols in last published order

    def rank(self, signals: list[StoredSignal]) -> list[RankedEntry]:
        actionable = [item for item in signals if item.state in ACTIONABLE and item.opportunity_score >= self.min_score]
        by_symbol: dict[str, list[StoredSignal]] = {}
        for item in actionable:
            by_symbol.setdefault(item.symbol, []).append(item)
        best: list[tuple[float, StoredSignal, list[str]]] = []
        for items in by_symbol.values():
            directions = {item.direction for item in items}
            items.sort(key=lambda item: (-item.opportunity_score, item.signal_key))
            leader = items[0]
            score = leader.opportunity_score
            flags: list[str] = []
            if len(directions) > 1:
                score *= 0.7
                flags.append("conflicting_events_for_symbol")
            else:
                aligned_extra = len({item.event_id for item in items}) - 1
                if aligned_extra:
                    score = min(100.0, score + self.multiple_event_bonus * min(aligned_extra, 2))
                    flags.append(f"{aligned_extra + 1}_aligned_events")
            if score >= self.min_score:
                best.append((score, leader, flags))
        incumbents = set(self.previous)

        def sort_key(entry: tuple[float, StoredSignal, list[str]]) -> tuple[float, str]:
            score, leader, _ = entry
            bonus = self.swap_margin if leader.symbol in incumbents else 0.0
            return (-(score + bonus), leader.symbol)

        best.sort(key=sort_key)
        result = []
        for index, (score, leader, flags) in enumerate(best[: self.top_n], start=1):
            payload = dict(leader.payload)
            payload["rank"] = index
            payload["ranking_score"] = round(score, 2)
            if flags:
                payload["risk_flags"] = list(dict.fromkeys(list(payload.get("risk_flags", [])) + flags))
            result.append(RankedEntry(index, leader.signal_key, leader.symbol, round(score, 2), payload))
        self.previous = [entry.symbol for entry in result]
        return result
