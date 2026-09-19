from datetime import datetime, timedelta, timezone

from psygridevents.asset_mechanism import AssetMechanismMapping
from psygridevents.event_timing import EventTimingAssessment
from psygridevents.exhaustion import ExhaustionAssessment
from psygridevents.market_confirmation import MarketConfirmationAssessment
from psygridevents.market_response import MarketResponseAssessment
from psygridevents.semantic import SemanticEvent
from psygridevents.signal_engine import SignalEngine

T0 = datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc)


def event(**overrides) -> SemanticEvent:
    values = dict(
        event_id="event-1", story_id="story-1", event_type="order", trigger="order awarded",
        event_time=T0, instruments=("RELIANCE",), participants=("reliance",),
        magnitude=None, direct_effect="revenue increase expected", indirect_effect=None,
        competitor_effect=None, supply_chain_effect=None, time_horizon="intraday",
        novelty_status="new", surprise_status="not_assessed", modality="asserted",
        negated=False, extraction_confidence=0.9, evidence=(), uncertainty=(), market_mechanism=None,
        materiality_status="high", materiality_score=0.9,
    )
    values.update(overrides)
    return SemanticEvent(**values)


def mapping(**overrides) -> AssetMechanismMapping:
    values = dict(
        event_id="event-1", asset="RELIANCE", asset_type="instrument", exposure_type="direct",
        mechanism="explicitly_named_in_event", expected_direction="positive", confidence=0.9,
        evidence=(), uncertainty=(), resolved=True, basis="source_entity_resolution",
    )
    values.update(overrides)
    return AssetMechanismMapping(**values)


def timing(**overrides) -> EventTimingAssessment:
    values = dict(
        event_id="event-1", event_time=T0, as_of=T0, age_hours=1.0, state="early",
        novelty_status="new", is_repackaged=False, uncertainty=(), reason="test",
    )
    values.update(overrides)
    return EventTimingAssessment(**values)


def response(**overrides) -> MarketResponseAssessment:
    values = dict(
        event_id="event-1", asset="RELIANCE", as_of=T0, expected_direction="positive",
        alignment="aligned", price_displacement=0.008, peak_aligned_displacement=0.008,
        relative_performance=0.005, sector_relative_performance=0.004, volume_ratio=1.5,
        volume_state="elevated", vwap_state="aligned", velocity_state="steady",
        reversal=False, response_state="early", observations_used=2,
        baseline_timestamp=T0, latest_timestamp=T0, uncertainty=(), reason="test",
    )
    values.update(overrides)
    return MarketResponseAssessment(**values)


def exhaustion(**overrides) -> ExhaustionAssessment:
    values = dict(
        event_id="event-1", state="early", timing_state="early", market_response_state="early",
        reversal=False, evidence=(), uncertainty=(), reason="test",
    )
    values.update(overrides)
    return ExhaustionAssessment(**values)


def confirmation(**overrides) -> MarketConfirmationAssessment:
    values = dict(
        event_id="event-1", status="untested", expected_direction="positive", observed_return=None,
        relative_return=None, volume_ratio=None, vwap_relation="not_available",
        sector_alignment="not_available", observations_used=0, window_start=None, window_end=None,
        score=0.0, reason="test",
    )
    values.update(overrides)
    return MarketConfirmationAssessment(**values)


def assess(evt=None, *, asset_mapping="default", tim="default", resp="default", exh="default", conf="default"):
    engine = SignalEngine()
    return engine.assess(
        evt or event(),
        story_id="story-1",
        asset_mapping=mapping() if asset_mapping == "default" else asset_mapping,
        timing=timing() if tim == "default" else tim,
        response=response() if resp == "default" else resp,
        exhaustion=exhaustion() if exh == "default" else exh,
        confirmation=confirmation() if conf == "default" else conf,
        as_of=T0,
    )


def test_unresolved_asset_yields_no_signal() -> None:
    assessment = assess(asset_mapping=None)
    assert assessment.signal_state == "NO_SIGNAL"
    assert assessment.asset is None


def test_negated_event_yields_no_signal() -> None:
    assessment = assess(event(negated=True, modality="negated"))
    assert assessment.signal_state == "NO_SIGNAL"


def test_neutral_direction_yields_no_signal() -> None:
    assessment = assess(asset_mapping=mapping(expected_direction="neutral"))
    assert assessment.signal_state == "NO_SIGNAL"


def test_unknown_materiality_yields_no_signal() -> None:
    assessment = assess(event(materiality_status="unknown", materiality_score=0.0))
    assert assessment.signal_state == "NO_SIGNAL"


def test_unsupported_mechanism_yields_no_signal() -> None:
    assessment = assess(asset_mapping=mapping(mechanism=None))
    assert assessment.signal_state == "NO_SIGNAL"


def test_no_market_response_yet_is_watch() -> None:
    assessment = assess(resp=None, exh=exhaustion(state="unknown", market_response_state="unknown"))
    assert assessment.signal_state == "WATCH"


def test_late_response_without_corroboration_is_watch() -> None:
    assessment = assess(
        resp=response(response_state="late"),
        exh=exhaustion(state="late", market_response_state="late"),
    )
    assert assessment.signal_state == "WATCH"


def test_early_positive_response_is_early_long() -> None:
    assessment = assess()
    assert assessment.signal_state == "EARLY_LONG"
    assert assessment.expected_direction == "positive"
    assert 0.0 < assessment.signal_strength_or_confidence <= 1.0


def test_early_negative_response_is_early_short() -> None:
    assessment = assess(
        asset_mapping=mapping(expected_direction="negative"),
        resp=response(expected_direction="negative", response_state="early"),
        exh=exhaustion(timing_state="early", market_response_state="early"),
    )
    assert assessment.signal_state == "EARLY_SHORT"


def test_independent_confirmation_yields_confirmed() -> None:
    assessment = assess(conf=confirmation(status="confirmed", reason="corroborated"))
    assert assessment.signal_state == "CONFIRMED"


def test_contradicted_market_reaction_yields_invalidated() -> None:
    assessment = assess(conf=confirmation(status="contradicted", reason="opposite reaction"))
    assert assessment.signal_state == "INVALIDATED"


def test_exhausted_state_overrides_early_response() -> None:
    assessment = assess(exh=exhaustion(state="exhausted"))
    assert assessment.signal_state == "EXHAUSTED"
    assert assessment.signal_strength_or_confidence == 0.0


def test_exhausted_state_overrides_even_confirmation() -> None:
    assessment = assess(
        exh=exhaustion(state="exhausted"),
        conf=confirmation(status="confirmed"),
    )
    assert assessment.signal_state == "EXHAUSTED"


def test_signal_does_not_require_maximum_price_movement() -> None:
    # A small (0.008) early aligned move is enough for EARLY_LONG; the engine
    # must not wait for a large/maximal favorable excursion.
    assessment = assess(resp=response(price_displacement=0.003, response_state="early"))
    assert assessment.signal_state == "EARLY_LONG"


def test_signal_strength_is_independent_of_priority_score() -> None:
    # SignalEngine.assess never receives a PriorityAssessment/priority_score at
    # all, so two identical scenarios must yield identical signal strength
    # regardless of what an external (unrelated) priority score might be.
    first = assess()
    second = assess()
    assert first.signal_strength_or_confidence == second.signal_strength_or_confidence
    assert not hasattr(first, "priority_score")


def test_signal_id_is_deterministic_for_same_inputs() -> None:
    first = assess()
    second = assess()
    assert first.signal_id == second.signal_id


def test_signal_id_changes_with_as_of() -> None:
    engine = SignalEngine()
    first = engine.assess(
        event(), story_id="story-1", asset_mapping=mapping(), timing=timing(),
        response=response(), exhaustion=exhaustion(), confirmation=confirmation(), as_of=T0,
    )
    from datetime import timedelta
    second = engine.assess(
        event(), story_id="story-1", asset_mapping=mapping(), timing=timing(),
        response=response(), exhaustion=exhaustion(), confirmation=confirmation(),
        as_of=T0 + timedelta(minutes=5),
    )
    assert first.signal_id != second.signal_id


def test_uncertainty_and_provenance_are_carried_through() -> None:
    assessment = assess(event(evidence=(), uncertainty=("note",)))
    assert "note" in assessment.uncertainty
    assert assessment.source_references == ()


def test_trigger_and_invalidation_are_always_populated() -> None:
    for state_kwargs in [
        {},
        {"asset_mapping": None},
        {"exh": exhaustion(state="exhausted")},
        {"conf": confirmation(status="contradicted")},
    ]:
        assessment = assess(**state_kwargs)
        assert assessment.trigger
        assert assessment.invalidation


def test_market_observation_timestamp_and_freshness_are_exposed_live() -> None:
    assessment = assess(resp=response(latest_timestamp=T0), tim=timing(as_of=T0))
    assert assessment.market_observation_timestamp == T0
    assert assessment.market_data_freshness == "LIVE"


def test_market_data_freshness_is_stale_when_old() -> None:
    old_ts = T0 - timedelta(hours=2)
    assessment = assess(resp=response(latest_timestamp=old_ts))
    assert assessment.market_observation_timestamp == old_ts
    assert assessment.market_data_freshness == "STALE"


def test_market_data_freshness_is_no_data_without_a_market_response() -> None:
    assessment = assess(resp=None, exh=exhaustion(state="unknown", market_response_state="unknown"))
    assert assessment.market_observation_timestamp is None
    assert assessment.market_data_freshness == "NO_DATA"


def test_market_session_state_defaults_to_unknown_when_not_supplied() -> None:
    assessment = assess()
    assert assessment.market_session_state == "UNKNOWN"
