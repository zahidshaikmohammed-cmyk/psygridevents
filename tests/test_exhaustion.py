from datetime import datetime, timezone

from psygridevents.event_timing import EventTimingAssessment
from psygridevents.exhaustion import ExhaustionEngine
from psygridevents.market_response import MarketResponseAssessment

T0 = datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc)


def timing(state: str, *, age_hours: float | None = 1.0) -> EventTimingAssessment:
    return EventTimingAssessment(
        event_id="event-1", event_time=T0, as_of=T0, age_hours=age_hours, state=state,
        novelty_status="new", is_repackaged=False, uncertainty=(), reason="test",
    )


def response(state: str, *, reversal: bool = False, displacement: float | None = 0.01) -> MarketResponseAssessment:
    return MarketResponseAssessment(
        event_id="event-1", asset="RELIANCE", as_of=T0, expected_direction="positive",
        alignment="aligned" if state != "no_response" else "flat",
        price_displacement=displacement, peak_aligned_displacement=displacement,
        relative_performance=None, sector_relative_performance=None, volume_ratio=None,
        volume_state="unavailable", vwap_state="unavailable", velocity_state="unavailable",
        reversal=reversal, response_state=state, observations_used=2,
        baseline_timestamp=T0, latest_timestamp=T0, uncertainty=(), reason="test",
    )


def test_fresh_timing_and_early_response_is_early() -> None:
    engine = ExhaustionEngine()
    assessment = engine.assess(timing("new"), response("early"))
    assert assessment.state == "early"


def test_stale_timing_overrides_a_technically_early_price_move() -> None:
    engine = ExhaustionEngine()
    assessment = engine.assess(timing("late"), response("early"))
    assert assessment.state == "late"


def test_market_exhaustion_overrides_fresh_timing() -> None:
    engine = ExhaustionEngine()
    assessment = engine.assess(timing("new"), response("exhausted", reversal=True))
    assert assessment.state == "exhausted"
    assert assessment.reversal is True


def test_no_market_data_falls_back_to_timing_alone() -> None:
    engine = ExhaustionEngine()
    assessment = engine.assess(timing("developing"), None)
    assert assessment.state == "developing"
    assert "based on event timing only" in " ".join(assessment.uncertainty)


def test_both_unknown_yields_unknown() -> None:
    engine = ExhaustionEngine()
    unknown_timing = EventTimingAssessment(
        event_id="event-1", event_time=None, as_of=T0, age_hours=None, state="unknown",
        novelty_status="new", is_repackaged=False, uncertainty=(), reason="test",
    )
    unknown_response = MarketResponseAssessment(
        event_id="event-1", asset="RELIANCE", as_of=T0, expected_direction="unknown",
        alignment="unknown", price_displacement=None, peak_aligned_displacement=None,
        relative_performance=None, sector_relative_performance=None, volume_ratio=None,
        volume_state="unavailable", vwap_state="unavailable", velocity_state="unavailable",
        reversal=False, response_state="unknown", observations_used=0,
        baseline_timestamp=None, latest_timestamp=None, uncertainty=(), reason="test",
    )
    assessment = engine.assess(unknown_timing, unknown_response)
    assert assessment.state == "unknown"


def test_developing_response_with_developing_timing_stays_developing() -> None:
    engine = ExhaustionEngine()
    assessment = engine.assess(timing("developing"), response("developing"))
    assert assessment.state == "developing"
