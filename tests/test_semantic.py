from datetime import datetime, timezone
from pathlib import Path

from psygridevents.acquisition import RawObservation
from psygridevents.clustering import StoryCluster
from psygridevents.entity_resolution import EntityMatch
from psygridevents.evidence import assess_evidence
from psygridevents.semantic import SemanticExtractor
from psygridevents.story_engine import StoryIntelligence


ROOT = Path(__file__).resolve().parents[1]


def _intelligence(title: str, summary: str = "") -> StoryIntelligence:
    timestamp = datetime(2026, 9, 17, 9, 0, tzinfo=timezone.utc)
    observation = RawObservation(
        provider_id="test",
        source_tier=0,
        publisher="Test Source",
        title=title,
        url="https://example.com/1",
        summary=summary,
        published_at=timestamp,
        observed_at=timestamp,
        raw={},
    )
    story = StoryCluster(
        cluster_id="story-000001",
        observations=(observation,),
        representative=observation,
        similarity_basis="test",
    )
    entity = EntityMatch("RELIANCE", "reliance", 0, 8, 0.99)
    return StoryIntelligence(story=story, entities=(entity,), evidence=assess_evidence([observation]))


def test_semantic_event_extracts_type_magnitude_and_provenance() -> None:
    extractor = SemanticExtractor(ROOT / "config" / "semantic_rules.yaml")
    intelligence = _intelligence("RELIANCE wins order worth INR 500 crore")

    events = extractor.extract(intelligence)

    assert len(events) == 1
    event = events[0]
    assert event.event_type == "order"
    assert event.instruments == ("RELIANCE",)
    assert event.magnitude is not None
    assert event.magnitude.normalized_value == 500.0
    assert event.evidence[0].observation_url == "https://example.com/1"
    assert event.novelty_status == "not_assessed"
    assert event.surprise_status == "not_assessed"


def test_semantic_engine_does_not_invent_market_direction() -> None:
    extractor = SemanticExtractor(ROOT / "config" / "semantic_rules.yaml")
    intelligence = _intelligence("RELIANCE wins order worth INR 500 crore")

    event = extractor.extract(intelligence)[0]

    assert event.market_mechanism is None
    assert event.direct_effect is None


def test_hypothetical_language_is_retained_as_uncertainty() -> None:
    extractor = SemanticExtractor(ROOT / "config" / "semantic_rules.yaml")
    intelligence = _intelligence("RELIANCE may acquire a stake")

    event = extractor.extract(intelligence)[0]

    assert event.event_type == "merger_acquisition"
    assert event.modality == "hypothetical"
    assert any("hypothetical" in item for item in event.uncertainty)
