from datetime import datetime, timedelta, timezone
from pathlib import Path

from psygridevents.event_timing import EventTimingEngine
from psygridevents.semantic import SemanticEvent

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "config" / "event_timing_rules.yaml"
T0 = datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc)


def event(**overrides) -> SemanticEvent:
    values = dict(
        event_id="event-1", story_id="story-1", event_type="order", trigger="order awarded",
        event_time=T0, instruments=("RELIANCE",), participants=("reliance",),
        magnitude=None, direct_effect=None, indirect_effect=None, competitor_effect=None,
        supply_chain_effect=None, time_horizon=None, novelty_status="new",
        surprise_status="not_assessed", modality="asserted", negated=False,
        extraction_confidence=0.9, evidence=(), uncertainty=(), market_mechanism=None,
    )
    values.update(overrides)
    return SemanticEvent(**values)


def test_very_recent_event_is_new() -> None:
    engine = EventTimingEngine(RULES)
    assessment = engine.assess(event(), as_of=T0 + timedelta(minutes=5))
    assert assessment.state == "new"


def test_event_a_few_hours_old_is_early() -> None:
    engine = EventTimingEngine(RULES)
    assessment = engine.assess(event(), as_of=T0 + timedelta(hours=2))
    assert assessment.state == "early"


def test_event_within_a_day_is_developing() -> None:
    engine = EventTimingEngine(RULES)
    assessment = engine.assess(event(), as_of=T0 + timedelta(hours=10))
    assert assessment.state == "developing"


def test_event_within_a_few_days_is_late() -> None:
    engine = EventTimingEngine(RULES)
    assessment = engine.assess(event(), as_of=T0 + timedelta(hours=48))
    assert assessment.state == "late"


def test_very_old_event_is_exhausted_by_pure_age() -> None:
    engine = EventTimingEngine(RULES)
    assessment = engine.assess(event(), as_of=T0 + timedelta(hours=200))
    assert assessment.state == "exhausted"


def test_missing_event_timestamp_is_handled_safely() -> None:
    engine = EventTimingEngine(RULES)
    assessment = engine.assess(event(event_time=None), as_of=T0)
    assert assessment.state == "unknown"
    assert assessment.age_hours is None


def test_repeated_novelty_does_not_restart_the_early_window() -> None:
    engine = EventTimingEngine(RULES)
    assessment = engine.assess(event(novelty_status="repeat"), as_of=T0 + timedelta(minutes=5))
    assert assessment.state == "late"
    assert assessment.is_repackaged is True


def test_stale_repackaged_novelty_is_exhausted_even_if_recently_republished() -> None:
    engine = EventTimingEngine(RULES)
    assessment = engine.assess(event(novelty_status="stale_repackaged"), as_of=T0 + timedelta(minutes=5))
    assert assessment.state == "exhausted"
    assert assessment.is_repackaged is True


def test_negated_event_gets_no_timing_state() -> None:
    engine = EventTimingEngine(RULES)
    assessment = engine.assess(event(negated=True, modality="negated"), as_of=T0)
    assert assessment.state == "unknown"


def test_age_hours_is_calculated() -> None:
    engine = EventTimingEngine(RULES)
    assessment = engine.assess(event(), as_of=T0 + timedelta(hours=3))
    assert assessment.age_hours == 3.0
