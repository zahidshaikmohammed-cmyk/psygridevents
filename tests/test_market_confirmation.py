from datetime import datetime, timedelta, timezone
from pathlib import Path

from psygridevents.market_confirmation import MarketConfirmationEngine, MarketObservation
from psygridevents.semantic import SemanticEvent

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "config" / "market_confirmation_rules.yaml"
T0 = datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc)


def event(event_id: str, *, effect: str = "revenue increase", modality: str = "asserted") -> SemanticEvent:
    return SemanticEvent(
        event_id=event_id,
        story_id=f"story-{event_id}",
        event_type="order",
        trigger="order awarded",
        event_time=T0,
        instruments=("RELIANCE",),
        participants=("reliance",),
        magnitude=None,
        direct_effect=effect,
        indirect_effect=None,
        competitor_effect=None,
        supply_chain_effect=None,
        time_horizon="near_term",
        novelty_status="new",
        surprise_status="not_assessed",
        modality=modality,
        negated=False,
        extraction_confidence=0.9,
        evidence=(),
        uncertainty=(),
        market_mechanism=None,
    )


def obs(minutes: int, close: float, *, volume: float = 1000, vwap: float | None = None, benchmark_return: float | None = None, sector_return: float | None = None) -> MarketObservation:
    return MarketObservation(
        symbol="RELIANCE",
        timestamp=T0 + timedelta(minutes=minutes),
        open=close,
        high=close,
        low=close,
        close=close,
        volume=volume,
        vwap=vwap,
        average_volume=1000,
        benchmark_return=benchmark_return,
        sector_return=sector_return,
    )


def test_aligned_price_and_independent_dimensions_confirm() -> None:
    engine = MarketConfirmationEngine(RULES)
    assessment = engine.assess(
        event("positive"),
        (
            obs(-1, 100),
            obs(5, 100.8, volume=1500, vwap=100.2, benchmark_return=0.001, sector_return=0.006),
        ),
    )
    assert assessment.status == "confirmed"
    assert assessment.observed_return > 0
    assert assessment.volume_ratio == 1.5
    assert assessment.vwap_relation == "aligned"


def test_opposite_price_reaction_is_contradicted() -> None:
    engine = MarketConfirmationEngine(RULES)
    assessment = engine.assess(event("negative", effect="revenue decrease"), (obs(-1, 100), obs(5, 98.8)))
    assert assessment.status == "contradicted"
    assert assessment.expected_direction == "negative"


def test_missing_synchronized_baseline_is_untested() -> None:
    engine = MarketConfirmationEngine(RULES)
    assessment = engine.assess(event("missing"), (obs(5, 101),))
    assert assessment.status == "untested"
    assert assessment.observed_return is None


def test_neutral_event_is_not_forced_into_market_direction() -> None:
    engine = MarketConfirmationEngine(RULES)
    assessment = engine.assess(event("neutral", effect="business activity continued"), (obs(-1, 100), obs(5, 101)))
    assert assessment.status == "untested"
    assert assessment.expected_direction == "neutral"


def test_non_asserted_event_is_not_confirmed() -> None:
    engine = MarketConfirmationEngine(RULES)
    assessment = engine.assess(event("planned", modality="planned"), (obs(-1, 100), obs(5, 101)))
    assert assessment.status == "untested"
