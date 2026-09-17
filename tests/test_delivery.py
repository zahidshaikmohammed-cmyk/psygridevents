from datetime import datetime, timezone

from psygridevents.acquisition import RawObservation
from psygridevents.clustering import StoryCluster
from psygridevents.delivery import SCHEMA_VERSION, build_intelligence_payload
from psygridevents.evidence import EvidenceAssessment
from psygridevents.priority import PriorityAssessment
from psygridevents.semantic import SemanticEvent
from psygridevents.story_engine import StoryIntelligence


def _observation() -> RawObservation:
    now = datetime(2026, 9, 17, 8, tzinfo=timezone.utc)
    return RawObservation(
        provider_id="source-a",
        source_tier=0,
        publisher="Official Source",
        title="Reliance wins order",
        url="https://example.com/event",
        summary="Reliance wins order",
        published_at=now,
        observed_at=now,
        raw={"must_not_leak": "raw provider payload"},
    )


def _event() -> SemanticEvent:
    return SemanticEvent(
        event_id="evt-1",
        story_id="story-000001",
        event_type="order",
        trigger="order awarded",
        event_time=datetime(2026, 9, 17, 7, 59, tzinfo=timezone.utc),
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


def _priority() -> PriorityAssessment:
    return PriorityAssessment(
        event_id="evt-1",
        priority_score=82.5,
        priority_class="high",
        coverage=0.88,
        component_scores={"source_confidence": 1.0},
        available_factors=("source_confidence",),
        missing_factors=("surprise",),
        reason="1/8 priority factors available (88% model coverage); missing: surprise.",
    )


def _story() -> StoryIntelligence:
    observation = _observation()
    return StoryIntelligence(
        story=StoryCluster(
            cluster_id="story-000001",
            observations=(observation,),
            representative=observation,
            similarity_basis="test",
        ),
        entities=(),
        evidence=EvidenceAssessment(1, 1, 0, True, "first_party_evidence", ()),
        semantic_events=(_event(),),
        priorities=(_priority(),),
    )


def test_payload_has_stable_top_level_contract():
    payload = build_intelligence_payload(
        [_story()],
        [_priority()],
        generated_at=datetime(2026, 9, 17, 8, 1, tzinfo=timezone.utc),
    )

    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["engine"] == "psygridevents"
    assert payload["summary"] == {
        "story_count": 1,
        "event_count": 1,
        "ranked_event_count": 1,
    }
    assert payload["ranked_events"][0]["priority"]["event_id"] == "evt-1"
    assert payload["ranked_events"][0]["event"]["event_id"] == "evt-1"
    assert payload["ranked_events"][0]["story_id"] == "story-000001"


def test_payload_serializes_datetime_and_does_not_expose_raw_provider_payload():
    payload = build_intelligence_payload(
        [_story()], [], generated_at=datetime(2026, 9, 17, 8, 1, tzinfo=timezone.utc)
    )

    assert payload["generated_at"].endswith("+00:00")
    assert "raw" not in payload["stories"][0]["sources"][0]


def test_payload_preserves_priority_explainability():
    payload = build_intelligence_payload(
        [_story()], [_priority()], generated_at=datetime.now(timezone.utc)
    )

    delivered = payload["ranked_events"][0]["priority"]
    assert delivered["component_scores"]["source_confidence"] == 1.0
    assert delivered["missing_factors"] == ["surprise"]
    assert delivered["coverage"] == 0.88
