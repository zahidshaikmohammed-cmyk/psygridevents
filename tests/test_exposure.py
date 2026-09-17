from pathlib import Path

from psygridevents.exposure import ExposureGraph
from psygridevents.semantic import SemanticEvent


ROOT = Path(__file__).resolve().parents[1]


def test_exposure_graph_emits_direct_named_instrument_only_without_issuer_master() -> None:
    graph = ExposureGraph(ROOT / "config" / "exposure_rules.yaml")
    event = SemanticEvent(
        event_id="event-1",
        story_id="story-1",
        event_type="order",
        trigger="contract",
        event_time=None,
        instruments=("RELIANCE",),
        participants=("reliance",),
        magnitude=None,
        direct_effect=None,
        indirect_effect=None,
        competitor_effect=None,
        supply_chain_effect=None,
        time_horizon=None,
        novelty_status="not_assessed",
        surprise_status="not_assessed",
        modality="asserted",
        negated=False,
        extraction_confidence=0.8,
        evidence=(),
        uncertainty=(),
        market_mechanism=None,
    )

    links = graph.map_event(event)

    assert len(links) == 1
    assert links[0].target == "RELIANCE"
    assert links[0].relationship == "direct"
