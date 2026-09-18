from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path

from psygridevents.event_mapping import EventMechanismAssessment
from psygridevents.event_timing import EventTimingAssessment, EventTimingEngine, EventTimingState
from psygridevents.market_confirmation import MarketObservation
from psygridevents.semantic import EvidenceSpan, SemanticEvent
from psygridevents.signal import EventSignalEngine, SignalState

ROOT = Path(__file__).resolve().parents[1]

UTC = timezone.utc
T0 = datetime(2026, 9, 17, 9, 30, tzinfo=UTC)


def event(**kwargs):
    base = dict(
        event_id="event-test",
        story_id="story-test",
        event_type="order",
        trigger="order awarded",
        event_time=T0,
        instruments=("RELIANCE",),
        participants=("Reliance Industries",),
        magnitude=None,
        direct_effect="revenue growth",
        indirect_effect=None,
        competitor_effect=None,
        supply_chain_effect=None,
        time_horizon="near_term",
        novelty_status="new",
        surprise_status="not_assessed",
        modality="asserted",
        negated=False,
        extraction_confidence=0.95,
        evidence=(EvidenceSpan("https://example.com/a", "Test", "order awarded", 0),),
        uncertainty=(),
        market_mechanism=None,
        materiality_score=0.9,
    )
    return SemanticEvent(**{**base, **kwargs})


def mapping(direction="long", status="mapped"):
    return EventMechanismAssessment("event-test", status, ("RELIANCE",), direction, "order_to_revenue_backlog", 0.95, "verified mapping")


def obs(minutes, close, *, vwap=None, volume=100, average_volume=100, benchmark_return=None, sector_return=None):
    ts = T0 + timedelta(minutes=minutes)
    return MarketObservation("RELIANCE", ts, close, close, close, close, volume, vwap, average_volume, benchmark_return, sector_return)


def timing(as_of, state=EventTimingState.EARLY):
    return EventTimingAssessment("event-test", state, T0, T0, (as_of - T0).total_seconds(), (as_of - T0).total_seconds(), "new", False, "test")


def test_new_event_gets_new_state():
    assessment = EventTimingEngine().assess(event(), as_of=T0 + timedelta(minutes=1))
    assert assessment.state == EventTimingState.NEW
    assert assessment.event_age_seconds == 60


def test_missing_timestamp_is_unknown_not_fabricated():
    e = event(event_time=None)
    assessment = EventTimingEngine().assess(e, as_of=T0 + timedelta(minutes=1))
    assert assessment.state == EventTimingState.UNKNOWN
    assert assessment.event_age_seconds is None


def test_repackaged_event_marks_repeated():
    prior = event(event_id="prior")
    assessment = EventTimingEngine().assess(event(), as_of=T0 + timedelta(minutes=5), first_detection=T0 + timedelta(minutes=1), repeated=True)
    assert assessment.recurrence == "repeated"
    assert prior.event_type == "order"


def test_unknown_asset_never_signals():
    e = event(instruments=())
    result = EventSignalEngine().assess(e, mapping("long", "unresolved"), timing(T0 + timedelta(minutes=5)), [obs(0, 100), obs(2, 100.5, vwap=100)], as_of=T0 + timedelta(minutes=5))
    assert result.signal_state == SignalState.NO_SIGNAL
    assert result.asset is None


def test_missing_live_market_data_stays_watch():
    result = EventSignalEngine().assess(event(), mapping(), timing(T0 + timedelta(minutes=5)), [], as_of=T0 + timedelta(minutes=5))
    assert result.signal_state == SignalState.WATCH
    assert result.price_displacement is None


def test_early_bullish_response_emits_early_long_before_exhaustion():
    market = [obs(0, 100), obs(5, 100.3, vwap=100.1, volume=100, average_volume=100)]
    result = EventSignalEngine().assess(event(), mapping(), timing(T0 + timedelta(minutes=5)), market, as_of=T0 + timedelta(minutes=5))
    assert result.signal_state == SignalState.EARLY_LONG
    assert result.exhaustion_state == "NOT_EXHAUSTED"
    assert result.price_displacement == 0.003


def test_early_bearish_response_emits_early_short():
    e = event(direct_effect="revenue decline")
    market = [obs(0, 100), obs(5, 99.7, vwap=99.9, volume=150, average_volume=100)]
    result = EventSignalEngine().assess(e, mapping("short"), timing(T0 + timedelta(minutes=5)), market, as_of=T0 + timedelta(minutes=5))
    assert result.signal_state == SignalState.EARLY_SHORT


def test_already_large_move_is_exhausted_not_fresh_signal():
    market = [obs(0, 100), obs(8, 101.5, vwap=101, volume=200, average_volume=100)]
    result = EventSignalEngine().assess(event(), mapping(), timing(T0 + timedelta(minutes=8)), market, as_of=T0 + timedelta(minutes=8))
    assert result.signal_state == SignalState.EXHAUSTED
    assert result.exhaustion_state == "EXHAUSTED"


def test_contradictory_response_invalidates():
    e = event(direct_effect="revenue growth")
    market = [obs(0, 100), obs(5, 99.7, vwap=99.9, volume=150, average_volume=100)]
    result = EventSignalEngine().assess(e, mapping(), timing(T0 + timedelta(minutes=5)), market, as_of=T0 + timedelta(minutes=5))
    assert result.signal_state == SignalState.INVALIDATED


def test_priority_score_is_not_used_by_signal_engine():
    e = replace(event(), materiality_score=0.01)
    market = [obs(0, 100), obs(5, 100.3, vwap=100.1, volume=150, average_volume=100)]
    result = EventSignalEngine().assess(e, mapping(), timing(T0 + timedelta(minutes=5)), market, as_of=T0 + timedelta(minutes=5))
    assert result.signal_state == SignalState.EARLY_LONG


def test_no_timestamp_market_baseline_is_not_fabricated():
    market = [obs(-1, 100)]
    result = EventSignalEngine().assess(event(), mapping(), timing(T0 + timedelta(minutes=5)), market, as_of=T0 + timedelta(minutes=5))
    assert result.signal_state == SignalState.WATCH
    assert result.price_displacement is None
