from pathlib import Path

from psygridevents.direction import DirectionEngine
from psygridevents.semantic import SemanticEvent

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "config" / "direction_rules.yaml"


def event(**overrides) -> SemanticEvent:
    values = dict(
        event_id="event-1", story_id="story-1", event_type="order", trigger="order awarded",
        event_time=None, instruments=("RELIANCE",), participants=("reliance",),
        magnitude=None, direct_effect=None, indirect_effect=None, competitor_effect=None,
        supply_chain_effect=None, time_horizon=None, novelty_status="not_assessed",
        surprise_status="not_assessed", modality="asserted", negated=False,
        extraction_confidence=0.9, evidence=(), uncertainty=(), market_mechanism=None,
    )
    values.update(overrides)
    return SemanticEvent(**values)


def test_positive_documented_effect_yields_positive_direction() -> None:
    engine = DirectionEngine(RULES)
    assessment = engine.assess(event(direct_effect="revenue increase expected"))
    assert assessment.direction == "positive"
    assert assessment.basis == "direct_effect"


def test_negative_trigger_yields_negative_direction() -> None:
    engine = DirectionEngine(RULES)
    assessment = engine.assess(event(trigger="contract cancelled"))
    assert assessment.direction == "negative"


def test_no_polarity_signal_is_neutral_not_guessed() -> None:
    engine = DirectionEngine(RULES)
    assessment = engine.assess(event(trigger="business activity continued"))
    assert assessment.direction == "neutral"


def test_negated_event_is_unknown_not_forced() -> None:
    engine = DirectionEngine(RULES)
    assessment = engine.assess(event(negated=True, modality="negated"))
    assert assessment.direction == "unknown"


def test_planned_modality_is_unknown() -> None:
    engine = DirectionEngine(RULES)
    assessment = engine.assess(event(modality="planned", direct_effect="revenue increase expected"))
    assert assessment.direction == "unknown"
