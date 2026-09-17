from datetime import datetime, timezone

from psygridevents.acquisition import RawObservation
from psygridevents.clustering import StoryCluster, cluster_stories
from psygridevents.deduplication import deduplicate
from psygridevents.entity_resolution import InstrumentResolver
from psygridevents.evidence import EvidenceAssessment, assess_evidence
from psygridevents.semantic import SemanticEvent
from psygridevents.story_engine import StoryEngine, StoryIntelligence
from psygridevents.transmission import TransmissionAssessment


def observation(title: str, publisher: str = "Source A", url: str = "https://example.com/1") -> RawObservation:
    return RawObservation(
        provider_id=publisher.lower().replace(" ", "-"),
        source_tier=2,
        publisher=publisher,
        title=title,
        url=url,
        summary=title,
        published_at=datetime(2026, 9, 17, 8, 0, tzinfo=timezone.utc),
        observed_at=datetime(2026, 9, 17, 8, 1, tzinfo=timezone.utc),
        raw={},
    )


def test_dedup_keeps_exact_duplicate_out():
    items = [
        observation("Company wins major order", "Wire", "https://example.com/a"),
        observation("Company wins major order", "Wire", "https://example.com/a"),
    ]
    decisions = deduplicate(items)
    assert sum(item.duplicate_of is None for item in decisions) == 1


def test_story_clustering_groups_related_coverage():
    items = [
        observation("Reliance wins major telecom order"),
        observation("Reliance wins major telecom contract", "Wire B", "https://example.com/b"),
    ]
    stories = cluster_stories(items, threshold=0.30)
    assert len(stories) == 1
    assert len(stories[0].observations) == 2


def test_entity_resolver_is_conservative():
    resolver = InstrumentResolver({"RELIANCE": ["Reliance Industries"]})
    matches = resolver.resolve("Reliance Industries announces a new project")
    assert [match.instrument for match in matches] == ["RELIANCE"]


def test_evidence_does_not_count_same_publisher_as_independent():
    items = [
        observation("A", "Same Publisher", "https://example.com/a"),
        observation("B", "Same Publisher", "https://example.com/b"),
    ]
    assessment = assess_evidence(items)
    assert assessment.independent_publisher_count == 1
    assert assessment.corroboration_state == "single_source"


def test_story_engine_attaches_cp6_priorities():
    engine = StoryEngine("config/instruments.json")
    obs = observation("Reliance wins major order")
    story = StoryCluster(
        cluster_id="story-000001",
        observations=(obs,),
        representative=obs,
        similarity_basis="test",
    )
    event = SemanticEvent(
        event_id="evt-1",
        story_id="story-000001",
        event_type="order",
        trigger="order awarded",
        event_time=obs.published_at,
        instruments=("RELIANCE",),
        participants=("Reliance Industries",),
        magnitude=None,
        direct_effect="positive revenue impact",
        indirect_effect=None,
        competitor_effect=None,
        supply_chain_effect=None,
        time_horizon="intraday",
        novelty_status="new",
        surprise_status="not_assessed",
        modality="asserted",
        negated=False,
        extraction_confidence=0.9,
        evidence=(),
        uncertainty=(),
        market_mechanism="revenue transmission",
        novelty_score=0.9,
        materiality_status="high",
        materiality_score=0.9,
    )
    intelligence = [
        StoryIntelligence(
            story=story,
            entities=(),
            evidence=EvidenceAssessment(
                source_count=1,
                independent_publisher_count=1,
                best_source_tier=2,
                first_party_present=False,
                corroboration_state="single_source",
                notes=(),
            ),
            semantic_events=(event,),
            transmissions=(
                TransmissionAssessment(
                    event_id="evt-1",
                    status="unlinked",
                    links=(),
                    confidence=0.0,
                    reason="test",
                ),
            ),
        )
    ]

    result = engine.build_prioritization(intelligence)

    assert len(result[0].priorities) == 1
    assert result[0].priorities[0].event_id == "evt-1"
    assert 0 <= result[0].priorities[0].priority_score <= 100
    assert "surprise" in result[0].priorities[0].missing_factors
