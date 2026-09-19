from datetime import datetime, timedelta, timezone
from pathlib import Path

from psygridevents.market_confirmation import MarketObservation
from psygridevents.market_response import MarketResponseEngine

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "config" / "market_response_rules.yaml"
T0 = datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc)


def obs(minutes: int, close: float, *, volume: float = 1000.0, average_volume: float | None = 1000.0,
        vwap: float | None = None, benchmark_return: float | None = None, sector_return: float | None = None) -> MarketObservation:
    return MarketObservation(
        symbol="RELIANCE",
        timestamp=T0 + timedelta(minutes=minutes),
        open=close, high=close, low=close, close=close,
        volume=volume, vwap=vwap, average_volume=average_volume,
        benchmark_return=benchmark_return, sector_return=sector_return,
    )


def assess(observations, *, as_of_minutes: int, direction: str = "positive"):
    engine = MarketResponseEngine(RULES)
    return engine.assess(
        event_id="event-1",
        asset="RELIANCE",
        event_time=T0,
        expected_direction=direction,
        observations=observations,
        as_of=T0 + timedelta(minutes=as_of_minutes),
    )


def test_no_observations_after_event_is_no_response() -> None:
    assessment = assess([obs(-5, 100.0)], as_of_minutes=10)
    assert assessment.response_state == "no_response"


def test_missing_baseline_before_event_is_unknown() -> None:
    assessment = assess([obs(5, 101.0)], as_of_minutes=10)
    assert assessment.response_state == "unknown"


def test_small_move_below_threshold_is_no_response() -> None:
    assessment = assess([obs(-5, 100.0), obs(5, 100.05)], as_of_minutes=10)
    assert assessment.alignment == "flat"
    assert assessment.response_state == "no_response"


def test_early_aligned_move_is_early() -> None:
    assessment = assess([obs(-5, 100.0), obs(5, 100.5)], as_of_minutes=10)
    assert assessment.alignment == "aligned"
    assert assessment.response_state == "early"
    assert assessment.price_displacement == 0.005


def test_developing_aligned_move_is_developing() -> None:
    assessment = assess([obs(-5, 100.0), obs(5, 101.5)], as_of_minutes=10)
    assert assessment.response_state == "developing"


def test_late_aligned_move_is_late() -> None:
    assessment = assess([obs(-5, 100.0), obs(5, 102.5)], as_of_minutes=10)
    assert assessment.response_state == "late"


def test_extreme_displacement_without_reversal_is_exhausted() -> None:
    assessment = assess([obs(-5, 100.0), obs(5, 104.0)], as_of_minutes=10)
    assert assessment.response_state == "exhausted"
    assert assessment.reversal is False


def test_reversal_from_peak_marks_exhausted() -> None:
    observations = [obs(-5, 100.0), obs(5, 101.5), obs(10, 100.3)]
    assessment = assess(observations, as_of_minutes=15)
    assert assessment.reversal is True
    assert assessment.response_state == "exhausted"


def test_opposed_move_is_not_treated_as_an_aligned_response() -> None:
    assessment = assess([obs(-5, 100.0), obs(5, 99.0)], as_of_minutes=10)
    assert assessment.alignment == "opposed"
    assert assessment.response_state == "no_response"


def test_unestablished_direction_yields_unknown_response() -> None:
    assessment = assess([obs(-5, 100.0), obs(5, 100.5)], as_of_minutes=10, direction="neutral")
    assert assessment.response_state == "unknown"


def test_real_time_boundary_ignores_observations_after_as_of() -> None:
    observations = [obs(-5, 100.0), obs(5, 100.5), obs(60, 110.0)]
    assessment = assess(observations, as_of_minutes=10)
    assert assessment.latest_timestamp == T0 + timedelta(minutes=5)
    assert assessment.price_displacement == 0.005


def test_missing_volume_vwap_and_benchmark_are_reported_as_unavailable() -> None:
    assessment = assess(
        [obs(-5, 100.0, average_volume=None), obs(5, 100.5, average_volume=None)],
        as_of_minutes=10,
    )
    assert assessment.volume_state == "unavailable"
    assert assessment.vwap_state == "unavailable"
    assert assessment.relative_performance is None
    assert assessment.sector_relative_performance is None
    assert len(assessment.uncertainty) >= 2


def test_elevated_volume_is_detected() -> None:
    assessment = assess(
        [obs(-5, 100.0), obs(5, 100.5, volume=2000.0, average_volume=1000.0)],
        as_of_minutes=10,
    )
    assert assessment.volume_ratio == 2.0
    assert assessment.volume_state == "elevated"


def test_vwap_alignment_is_detected() -> None:
    assessment = assess(
        [obs(-5, 100.0), obs(5, 100.5, vwap=100.1)],
        as_of_minutes=10,
    )
    assert assessment.vwap_state == "aligned"
