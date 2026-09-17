from pathlib import Path

from psygridevents.contradiction import ContradictionEngine
from psygridevents.semantic import SemanticEvent

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "config" / "contradiction_rules.yaml"


def event(event_id: str, *, trigger: str, direct_effect: str | None = None, modality: str = "asserted") -> SemanticEvent:
    return SemanticEvent(
        event_id=event_id,
        story_id=f"story-{event_id}",
        event_type="order",
        trigger=trigger,
        event_time=None,
        instruments=("RELIANCE",),
        participants=("reliance",),
        magnitude=None,
        direct_effect=direct_effect,
        indirect_effect=None,
        competitor_effect=None,
        supply_chain_effect=None,
        time_horizon=None,
        novelty_status="not_assessed",
        surprise_status="not_assessed",
        modality=modality,
        negated=False,
        extraction_confidence=0.9,
        evidence=(),
        uncertainty=(),
        market_mechanism=None,
    )


def test_opposite_documented_effects_are_conflicted() -> None:
    engine = ContradictionEngine(RULES)
    current = event("current", trigger="order wins", direct_effect="revenue increase")
    previous = event("previous", trigger="order loss", direct_effect="revenue decrease")

    assessment = engine.assess(current, (previous,))

    assert assessment.status == "contradicted"
    assert assessment.narrative_state == "conflicted"
    assert assessment.matched_event_id == "previous"


def test_same_documented_effect_is_supported() -> None:
    engine = ContradictionEngine(RULES)
    current = event("current", trigger="order wins", direct_effect="revenue increase")
    previous = event("previous", trigger="contract awarded", direct_effect="revenue growth")

    assessment = engine.assess(current, (previous,))

    assert assessment.status == "corroborated"
    assert assessment.narrative_state == "supported"


def test_unrelated_event_does_not_create_conflict() -> None:
    engine = ContradictionEngine(RULES)
    current = event("current", trigger="order wins", direct_effect="revenue increase")
    previous = SemanticEvent(
        **{**current.__dict__, "event_id": "previous", "event_type": "legal", "trigger": "lawsuit"}
    )

    assessment = engine.assess(current, (previous,))

    assert assessment.status == "no_comparison"
    assert assessment.narrative_state == "new"


def test_non_asserted_event_is_not_classified_as_conflict() -> None:
    engine = ContradictionEngine(RULES)
    current = event("current", trigger="order wins", direct_effect="revenue increase", modality="planned")
    previous = event("previous", trigger="order loss", direct_effect="revenue decrease")

    assessment = engine.assess(current, (previous,))

    assert assessment.status == "not_assessed"
    assert assessment.narrative_state == "unresolved"
