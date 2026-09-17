from pathlib import Path

from psygridevents.semantic import SemanticEvent
from psygridevents.transmission import TransmissionEngine

ROOT = Path(__file__).resolve().parents[1]


def event(**overrides) -> SemanticEvent:
    values = dict(
        event_id="event-1", story_id="story-1", event_type="commodity", trigger="oil",
        event_time=None, instruments=("RELIANCE",), participants=("reliance",),
        magnitude=None, direct_effect=None, indirect_effect=None, competitor_effect=None,
        supply_chain_effect=None, time_horizon=None, novelty_status="not_assessed",
        surprise_status="not_assessed", modality="asserted", negated=False,
        extraction_confidence=0.9, evidence=(), uncertainty=(), market_mechanism=None,
    )
    values.update(overrides)
    return SemanticEvent(**values)


def test_direct_transmission_is_preserved_without_issuer_metadata() -> None:
    engine = TransmissionEngine(ROOT / "config" / "exposure_rules.yaml")
    assessment = engine.assess(event())
    assert assessment.status == "direct"
    assert assessment.links[0].target == "RELIANCE"
    assert assessment.confidence == 0.9


def test_second_order_transmission_requires_explicit_metadata(tmp_path) -> None:
    metadata = tmp_path / "issuers.yaml"
    metadata.write_text("issuers:\n  RELIANCE:\n    sectors: [energy]\n", encoding="utf-8")
    engine = TransmissionEngine(ROOT / "config" / "exposure_rules.yaml", metadata)
    assessment = engine.assess(event())
    assert assessment.status == "mixed"
    assert any(link.relationship == "input_cost" for link in assessment.links)


def test_missing_metadata_does_not_create_second_order_path() -> None:
    engine = TransmissionEngine(ROOT / "config" / "exposure_rules.yaml")
    assessment = engine.assess(event())
    assert all(link.relationship == "direct" for link in assessment.links)


def test_negated_event_is_blocked() -> None:
    engine = TransmissionEngine(ROOT / "config" / "exposure_rules.yaml")
    assessment = engine.assess(event(negated=True, modality="negated"))
    assert assessment.status == "blocked"
    assert assessment.links == ()


def test_planned_event_is_blocked() -> None:
    engine = TransmissionEngine(ROOT / "config" / "exposure_rules.yaml")
    assessment = engine.assess(event(modality="planned"))
    assert assessment.status == "blocked"
    assert assessment.links == ()
