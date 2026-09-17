from datetime import datetime, timezone

import pytest

from psygridevents.models import EventStatus, EventType, ImpactLevel
from psygridevents.pipeline import build_event, event_fingerprint
from psygridevents.scoring import ScoreInputs, calculate_priority
from psygridevents.universe import load_instruments


def test_universe_has_no_duplicates():
    instruments = load_instruments()
    assert len(instruments) == len(set(instruments))
    assert "RELIANCE" in instruments
    assert "HDFCBANK" in instruments


def test_event_fingerprint_is_stable():
    timestamp = datetime(2026, 9, 17, tzinfo=timezone.utc)
    first = event_fingerprint("Order win", "Example", timestamp)
    second = event_fingerprint("Order win", "Example", timestamp)
    assert first == second


def test_score_is_bounded_and_explainable():
    event = build_event(
        title="Material corporate event",
        summary="Confirmed event summary.",
        publisher="Exchange",
        url="https://example.invalid/event",
        event_type=EventType.CORPORATE,
        status=EventStatus.CONFIRMED,
        instruments=["RELIANCE"],
    )
    assessment = calculate_priority(
        event,
        ScoreInputs(
            source_confidence=1,
            novelty=1,
            surprise=0.8,
            financial_materiality=1,
            exposure=0.9,
            market_relevance=0.9,
            persistence=0.7,
            transmission=0.8,
        ),
    )
    assert 0 <= assessment.priority_score <= 100
    assert assessment.materiality == ImpactLevel.CRITICAL


def test_score_inputs_reject_invalid_values():
    with pytest.raises(ValueError):
        ScoreInputs(novelty=1.1)
