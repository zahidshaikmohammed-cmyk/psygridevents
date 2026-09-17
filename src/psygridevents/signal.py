from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Iterable

from .event_mapping import EventMechanismAssessment
from .event_timing import EventTimingAssessment, EventTimingState
from .market_confirmation import MarketObservation
from .semantic import SemanticEvent


class SignalState(StrEnum):
    NO_SIGNAL = "NO_SIGNAL"
    WATCH = "WATCH"
    EARLY_LONG = "EARLY_LONG"
    EARLY_SHORT = "EARLY_SHORT"
    CONFIRMED = "CONFIRMED"
    INVALIDATED = "INVALIDATED"
    EXHAUSTED = "EXHAUSTED"


@dataclass(frozen=True)
class MarketResponseAssessment:
    state: str
    baseline_price: float | None
    latest_price: float | None
    price_displacement: float | None
    relative_performance: float | None
    volume_ratio: float | None
    volume_state: str
    vwap_state: str
    rate_per_minute: float | None
    acceleration_state: str
    reversal_state: str
    observations_used: int
    reason: str


@dataclass(frozen=True)
class EventDrivenSignal:
    signal_id: str
    event_id: str
    story_id: str
    timestamp: datetime
    asset: str | None
    asset_type: str
    event_type: str
    event_age: float | None
    event_state: str
    expected_direction: str
    signal_state: SignalState
    signal_strength: float | None
    confidence: float | None
    materiality: float
    transmission_mechanism: str | None
    market_response: MarketResponseAssessment
    price_displacement: float | None
    relative_performance: float | None
    volume_state: str
    vwap_state: str
    exhaustion_state: str
    trigger: str | None
    invalidation: str | None
    evidence: tuple[object, ...]
    uncertainty: tuple[str, ...]
    source_references: tuple[object, ...]


class EventSignalEngine:
    """Separate actionable event-driven signaling from CP6 importance ranking."""

    def __init__(self, *, early_move: float = 0.002, exhaustion_move: float = 0.01, max_early_minutes: int = 30) -> None:
        self.early_move = early_move
        self.exhaustion_move = exhaustion_move
        self.max_early_minutes = max_early_minutes

    def assess(
        self,
        event: SemanticEvent,
        mapping: EventMechanismAssessment,
        timing: EventTimingAssessment,
        observations: Iterable[MarketObservation],
        *,
        as_of: datetime,
    ) -> EventDrivenSignal:
        obs = tuple(sorted((item for item in observations if item.symbol in mapping.assets), key=lambda item: item.timestamp))
        expected = mapping.direction
        response = self._market_response(event, expected, obs)
        if event.event_time is None or not event.instruments or mapping.status != "mapped" or expected not in {"long", "short"}:
            state = SignalState.NO_SIGNAL
            exhaustion = "UNKNOWN"
            reason = "Verified asset, timestamp, mechanism, and directional mapping are all required for an actionable signal."
        elif timing.state == EventTimingState.UNKNOWN or not obs:
            state = SignalState.WATCH
            exhaustion = "UNKNOWN"
            reason = "Signal remains on WATCH because timing or live market evidence is incomplete."
        elif abs(response.price_displacement or 0.0) >= self.exhaustion_move:
            state = SignalState.EXHAUSTED
            exhaustion = "EXHAUSTED"
            reason = "Observed event-time repricing has already become substantial; no fresh early signal is emitted."
        elif response.reversal_state == "invalidated":
            state = SignalState.INVALIDATED
            exhaustion = "REVERSING"
            reason = "Market response moved materially against the documented event direction."
        elif expected == "long" and (response.price_displacement or 0.0) >= self.early_move and response.vwap_state == "above" and response.reversal_state != "adverse":
            state = SignalState.EARLY_LONG
            exhaustion = "NOT_EXHAUSTED"
            reason = "Early directional displacement is present while the move remains inside the non-exhausted threshold."
        elif expected == "short" and (response.price_displacement or 0.0) <= -self.early_move and response.vwap_state == "below" and response.reversal_state != "adverse":
            state = SignalState.EARLY_SHORT
            exhaustion = "NOT_EXHAUSTED"
            reason = "Early directional displacement is present while the move remains inside the non-exhausted threshold."
        else:
            state = SignalState.WATCH
            exhaustion = "NOT_EXHAUSTED" if timing.state in {EventTimingState.NEW, EventTimingState.EARLY, EventTimingState.DEVELOPING} else "LATE"
            reason = "Event mapping exists, but the supplied market response does not yet satisfy the early-signal trigger."

        signal_id = f"signal-{event.event_id}-{as_of.astimezone().strftime('%Y%m%dT%H%M%S')}"
        strength = None if state in {SignalState.NO_SIGNAL, SignalState.WATCH} else min(1.0, round(abs(response.price_displacement or 0.0) / self.exhaustion_move, 3))
        confidence = None if state in {SignalState.NO_SIGNAL, SignalState.WATCH} else round(min(mapping.confidence, 1.0), 3)
        return EventDrivenSignal(
            signal_id=signal_id,
            event_id=event.event_id,
            story_id=event.story_id,
            timestamp=as_of,
            asset=mapping.assets[0] if mapping.assets else None,
            asset_type="NSE_INSTRUMENT" if mapping.assets else "UNKNOWN",
            event_type=event.event_type,
            event_age=timing.event_age_seconds,
            event_state=timing.state.value,
            expected_direction=expected,
            signal_state=state,
            signal_strength=strength,
            confidence=confidence,
            materiality=event.materiality_score,
            transmission_mechanism=mapping.mechanism,
            market_response=response,
            price_displacement=response.price_displacement,
            relative_performance=response.relative_performance,
            volume_state=response.volume_state,
            vwap_state=response.vwap_state,
            exhaustion_state=exhaustion,
            trigger=mapping.reason,
            invalidation=reason if state in {SignalState.INVALIDATED, SignalState.WATCH} else "Invalidate if price crosses event baseline or relative response reverses materially.",
            evidence=event.evidence,
            uncertainty=tuple(event.uncertainty) + (mapping.reason,),
            source_references=event.evidence,
        )

    def assess_many(
        self,
        events: Iterable[SemanticEvent],
        mappings: Iterable[EventMechanismAssessment],
        timings: Iterable[EventTimingAssessment],
        observations: Iterable[MarketObservation],
        *,
        as_of: datetime,
    ) -> tuple[EventDrivenSignal, ...]:
        mapping_by_id = {item.event_id: item for item in mappings}
        timing_by_id = {item.event_id: item for item in timings}
        market = tuple(observations)
        return tuple(
            self.assess(event, mapping_by_id.get(event.event_id, self._missing_mapping(event)), timing_by_id.get(event.event_id, self._missing_timing(event)), market, as_of=as_of)
            for event in events
        )

    @staticmethod
    def _missing_mapping(event: SemanticEvent) -> EventMechanismAssessment:
        return EventMechanismAssessment(event.event_id, "unresolved", (), "unknown", None, 0.0, "No event-to-asset mapping assessment supplied.")

    @staticmethod
    def _missing_timing(event: SemanticEvent) -> EventTimingAssessment:
        from .event_timing import EventTimingState
        return EventTimingAssessment(event.event_id, EventTimingState.UNKNOWN, None, event.event_time, None, None, "unknown", False, "No event timing assessment supplied.")

    def _market_response(self, event: SemanticEvent, expected: str, observations: tuple[MarketObservation, ...]) -> MarketResponseAssessment:
        if not observations:
            return MarketResponseAssessment("unavailable", None, None, None, None, None, "not_available", "not_available", None, "unknown", "unknown", 0, "No live market observations supplied.")
        baseline_candidates = [item for item in observations if item.timestamp <= event.event_time] if event.event_time else []
        post = [item for item in observations if event.event_time and item.timestamp > event.event_time]
        if not baseline_candidates or not post:
            return MarketResponseAssessment("insufficient", None, None, None, None, None, "not_available", "not_available", None, "unknown", "unknown", len(observations), "Event-time baseline and post-event observation are both required.")
        baseline = baseline_candidates[-1]
        latest = post[-1]
        displacement = latest.close / baseline.close - 1.0 if baseline.close > 0 else None
        relative = None if latest.benchmark_return is None or displacement is None else displacement - latest.benchmark_return
        volume_ratio = None if latest.average_volume is None or latest.average_volume <= 0 else latest.volume / latest.average_volume
        volume_state = "expanded" if volume_ratio is not None and volume_ratio >= 1.2 else "normal" if volume_ratio is not None else "not_available"
        vwap_state = "not_available" if latest.vwap is None else "above" if latest.close > latest.vwap else "below" if latest.close < latest.vwap else "at"
        minutes = max((latest.timestamp - baseline.timestamp).total_seconds() / 60.0, 0.001)
        rate = displacement / minutes if displacement is not None else None
        acceleration = "unknown"
        if len(post) >= 2 and displacement is not None:
            prior = post[-2]
            prior_displacement = prior.close / baseline.close - 1.0 if baseline.close > 0 else None
            if prior_displacement is not None:
                acceleration = "accelerating" if abs(displacement) > abs(prior_displacement) else "decelerating" if abs(displacement) < abs(prior_displacement) else "stable"
        reversal = "unknown"
        if displacement is not None:
            if (expected == "long" and displacement < 0) or (expected == "short" and displacement > 0):
                reversal = "invalidated"
            elif len(post) >= 2:
                prior = post[-2].close / baseline.close - 1.0 if baseline.close > 0 else 0.0
                reversal = "adverse" if (expected == "long" and displacement < prior) or (expected == "short" and displacement > prior) else "aligned"
        return MarketResponseAssessment("available", baseline.close, latest.close, round(displacement, 6) if displacement is not None else None, round(relative, 6) if relative is not None else None, round(volume_ratio, 3) if volume_ratio is not None else None, volume_state, vwap_state, round(rate, 6) if rate is not None else None, acceleration, reversal, len(observations), "Synchronized event-time market response measured without synthetic values.")
