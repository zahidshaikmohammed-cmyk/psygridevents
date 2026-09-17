from pathlib import Path

from psygridevents.materiality import MaterialityEngine
from psygridevents.semantic import Magnitude, SemanticEvent

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "config" / "materiality_rules.yaml"


def event(**overrides) -> SemanticEvent:
    values = dict(
        event_id="event-1",
        story_id="story-1",
        event_type="order",
        trigger="wins order",
        event_time=None,
        instruments=("RELIANCE",),
        participants=("RELIANCE",),
        magnitude=Magnitude("100 crore", "crore", 100.0, "crore", "100 crore"),
        direct_effect="revenue impact",
        indirect_effect=None,
        competitor_effect=None,
        supply_chain_effect=None,
        time_horizon="near_term",
        novelty_status="new",
        surprise_status="not_assessed",
        modality="asserted",
        negated=False,
        extraction_confidence=0.9,
        evidence=(),
        uncertainty=(),
        market_mechanism=None,
    )
    values.update(overrides)
    return SemanticEvent(**values)


def test_direct_quantified_event_is_material() -> None:
    assessment = MaterialityEngine(RULES).assess(event())
    assert assessment.status == "high"
    assert assessment.exposure == "direct"
    assert assessment.score >= 0.75


def test_no_instrument_fails_closed() -> None:
    assessment = MaterialityEngine(RULES).assess(event(instruments=()))
    assert assessment.status == "unknown"
    assert assessment.score == 0.0


def test_non_asserted_event_cannot_be_high() -> None:
    assessment = MaterialityEngine(RULES).assess(event(modality="hypothetical"))
    assert assessment.status == "low"
    assert assessment.score == 0.0


def test_event_without_magnitude_stays_below_high_without_effect() -> None:
    assessment = MaterialityEngine(RULES).assess(
        event(magnitude=None, direct_effect=None, novelty_status="not_assessed")
    )
    assert assessment.status == "medium"
    assert assessment.score < 0.75
