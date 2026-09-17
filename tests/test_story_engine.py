from datetime import datetime, timezone

from psygridevents.acquisition import RawObservation
from psygridevents.clustering import cluster_stories
from psygridevents.deduplication import deduplicate
from psygridevents.entity_resolution import InstrumentResolver
from psygridevents.evidence import assess_evidence


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
