from datetime import datetime, timezone

from psygridevents.evidence import EvidenceAssessment
from psygridevents.exposure import ExposureLink
from psygridevents.priority import PriorityEngine
from psygridevents.semantic import SemanticEvent
from psygridevents.transmission import TransmissionAssessment


def event(
    *,
    event_id="evt-1",
    event_type="order",
    novelty_status="new",
    novelty_score=0.9,
    surprise_status="not_assessed",
    materiality_status="high",
    materiality_score=0.9,
    time_horizon="intraday",
):
    return SemanticEvent(
        event_id=event_id,
        story_id="story-1",
        event_type=event_type,
        trigger="order awarded",
        event_time=datetime(2026, 9, 17, 3, tzinfo=timezone.utc),
        instruments=("RELIANCE",),
        participants=("Example Corp",),
        magnitude=None,
        direct_effect="positive revenue impact",
        indirect_effect=None,
        competitor_effect=None,
        supply_chain_effect=None,
        time_horizon=time_horizon,
        novelty_status=novelty_status,
        surprise_status=surprise_status,
        modality="asserted",
        negated=False,
        extraction_confidence=0.9,
        evidence=(),
        uncertainty=(),
        market_mechanism="revenue transmission",
        novelty_score=novelty_score,
        materiality_status=materiality_status,
        materiality_score=materiality_score,
    )


def evidence(tier=0):
    return EvidenceAssessment(
        source_count=1,
        independent_publisher_count=1,
        best_source_tier=tier,
        first_party_present=tier == 0,
        corroboration_state="first_party_evidence" if tier == 0 else "single_source",
        notes=(),
    )


def transmission(confidence=0.8, event_id="evt-1"):
    return TransmissionAssessment(
        event_id=event_id,
        status="direct",
        links=(
            ExposureLink(
                source="RELIANCE",
                target="RELIANCE",
                relationship="direct",
                mechanism="contract",
                horizon="intraday",
                confidence=confidence,
                basis="explicit instrument",
                evidence_required=True,
            ),
        ),
        confidence=confidence,
        reason="explicit",
    )


def test_priority_uses_only_available_factors_and_records_missing_data():
    engine = PriorityEngine("config/priority_rules.yaml")
    assessment = engine.assess(event(), evidence(), transmission())

    assert 0 <= assessment.priority_score <= 100
    assert "surprise" in assessment.missing_factors
    assert "surprise" not in assessment.component_scores
    assert assessment.coverage < 1.0
    assert "missing: surprise" in assessment.reason


def test_missing_factor_is_not_treated_as_zero():
    engine = PriorityEngine("config/priority_rules.yaml")
    complete = engine.assess(event(surprise_status="surprising_high"), evidence(), transmission())
    missing = engine.assess(event(), evidence(), transmission())

    assert complete.priority_score > missing.priority_score
    assert missing.component_scores["source_confidence"] == 1.0
    assert "surprise" in missing.missing_factors


def test_source_tier_changes_source_confidence():
    engine = PriorityEngine("config/priority_rules.yaml")
    first_party = engine.assess(event(), evidence(0), transmission())
    secondary = engine.assess(event(), evidence(3), transmission())

    assert first_party.component_scores["source_confidence"] > secondary.component_scores["source_confidence"]


def test_no_transmission_does_not_invent_exposure_or_transmission():
    engine = PriorityEngine("config/priority_rules.yaml")
    assessment = engine.assess(event(), evidence(), None)

    assert "exposure" in assessment.missing_factors
    assert "transmission" in assessment.missing_factors
    assert "exposure" not in assessment.component_scores
    assert "transmission" not in assessment.component_scores


def test_surprise_status_is_only_scored_when_explicitly_assessed():
    engine = PriorityEngine("config/priority_rules.yaml")

    high = engine.assess(event(surprise_status="surprising_high"), evidence(), transmission())
    low = engine.assess(event(surprise_status="surprising_low"), evidence(), transmission())
    inline = engine.assess(event(surprise_status="in_line"), evidence(), transmission())
    unknown = engine.assess(event(surprise_status="unknown"), evidence(), transmission())

    assert high.component_scores["surprise"] == 1.0
    assert low.component_scores["surprise"] == 1.0
    assert inline.component_scores["surprise"] == 0.2
    assert "surprise" in unknown.missing_factors


def test_unknown_time_horizon_is_not_scored_as_persistence():
    engine = PriorityEngine("config/priority_rules.yaml")
    assessment = engine.assess(event(time_horizon="unknown"), evidence(), transmission())

    assert "persistence" in assessment.missing_factors
    assert "persistence" not in assessment.component_scores


def test_contradiction_does_not_get_hidden_inside_priority_score():
    engine = PriorityEngine("config/priority_rules.yaml")
    assessment = engine.assess(event(), evidence(), transmission())

    assert "contradiction" not in assessment.component_scores


def test_rank_is_deterministic_with_tie_breaking():
    engine = PriorityEngine("config/priority_rules.yaml")
    first = engine.assess(event(event_id="a"), evidence(), transmission(event_id="a"))
    second = engine.assess(event(event_id="b"), evidence(), transmission(event_id="b"))

    ranked = engine.rank([second, first])

    assert [item.event_id for item in ranked] == ["b", "a"]


def test_high_class_requires_sufficient_coverage():
    engine = PriorityEngine("config/priority_rules.yaml")
    assessment = engine.assess(event(surprise_status="surprising_high"), evidence(), transmission())

    assert assessment.priority_class in {"high", "critical"}
    assert assessment.coverage >= 0.75


def test_assess_many_preserves_event_order():
    engine = PriorityEngine("config/priority_rules.yaml")
    events = [event(event_id="a"), event(event_id="b")]

    assessments = engine.assess_many(
        events,
        evidence(),
        [transmission(event_id="a"), transmission(event_id="b")],
    )

    assert [item.event_id for item in assessments] == ["a", "b"]
