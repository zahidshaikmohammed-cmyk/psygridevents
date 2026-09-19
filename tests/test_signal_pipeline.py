from datetime import datetime, timedelta, timezone
from pathlib import Path

from psygridevents.acquisition import RawObservation
from psygridevents.delivery import build_intelligence_payload
from psygridevents.issuer_master import IssuerMasterBuilder
from psygridevents.market_confirmation import MarketObservation
from psygridevents.story_engine import StoryEngine

ROOT = Path(__file__).resolve().parents[1]
INSTRUMENTS = ROOT / "config" / "instruments.json"
T0 = datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc)


def observation(title: str, *, url: str = "https://example.com/1", tier: int = 0) -> RawObservation:
    return RawObservation(
        provider_id="test", source_tier=tier, publisher="Official Source", title=title,
        url=url, summary=title, published_at=T0, observed_at=T0, raw={},
    )


def market_obs(minutes: int, close: float, **kwargs) -> MarketObservation:
    return MarketObservation(
        symbol="RELIANCE", timestamp=T0 + timedelta(minutes=minutes),
        open=close, high=close, low=close, close=close,
        volume=kwargs.pop("volume", 1000.0), average_volume=kwargs.pop("average_volume", None),
        **kwargs,
    )


def _run_pipeline(as_of: datetime, market_observations: list[MarketObservation]):
    engine = StoryEngine(INSTRUMENTS)
    observations = [observation("RELIANCE wins order worth INR 500 crore")]
    stories = engine.build_stories(observations)
    stories = engine.build_materiality(stories)
    stories = engine.build_prioritization(stories)
    stories = engine.build_market_confirmation(stories, market_observations)
    stories = engine.build_asset_mechanism(stories)
    stories = engine.build_event_timing(stories, as_of=as_of)
    stories = engine.build_market_response(stories, market_observations, as_of=as_of)
    stories = engine.build_exhaustion(stories)
    stories = engine.build_signals(stories, as_of=as_of)
    return engine, stories


def test_end_to_end_material_order_event_with_early_response_yields_early_long() -> None:
    # Price-only, uncorroborated reaction: CP5 market confirmation stays
    # "mixed" (no independent volume/VWAP/benchmark/sector corroboration),
    # so CP11 should settle on EARLY_LONG rather than jumping to CONFIRMED.
    market_observations = [market_obs(-5, 1000.0), market_obs(5, 1006.0)]
    engine, stories = _run_pipeline(T0 + timedelta(hours=1), market_observations)

    assert len(stories) == 1
    story = stories[0]
    assert len(story.signals) == 1
    signal = story.signals[0]

    assert signal.asset == "RELIANCE"
    assert signal.asset_type == "instrument"
    assert signal.expected_direction == "positive"
    assert signal.event_state in ("new", "early")
    assert signal.market_response == "early"
    assert signal.exhaustion_state == "early"
    assert signal.signal_state == "EARLY_LONG"
    assert signal.timestamp == T0 + timedelta(hours=1)
    # The signal fires while the event is still fresh and the move has not
    # exhausted -- exactly the "early, not exhausted" requirement.
    assert signal.event_age is not None and signal.event_age < 4.0


def test_end_to_end_with_no_market_data_yields_watch_not_a_guessed_direction() -> None:
    engine, stories = _run_pipeline(T0 + timedelta(hours=1), [])
    signal = stories[0].signals[0]
    assert signal.signal_state == "WATCH"
    assert signal.market_response == "unknown"


def test_end_to_end_stale_repackaged_story_never_gets_a_fresh_early_signal() -> None:
    engine = StoryEngine(INSTRUMENTS)
    from dataclasses import replace

    observations = [observation("RELIANCE wins order worth INR 500 crore")]
    stories = engine.build_stories(observations)
    stories = engine.build_materiality(stories)
    # Simulate that novelty assessment already marked this as stale/repackaged.
    stories = [
        replace(
            item,
            semantic_events=tuple(replace(e, novelty_status="stale_repackaged") for e in item.semantic_events),
        )
        for item in stories
    ]
    market_observations = [market_obs(-5, 1000.0), market_obs(5, 1006.0)]
    stories = engine.build_market_confirmation(stories, market_observations)
    stories = engine.build_asset_mechanism(stories)
    stories = engine.build_event_timing(stories, as_of=T0 + timedelta(minutes=10))
    stories = engine.build_market_response(stories, market_observations, as_of=T0 + timedelta(minutes=10))
    stories = engine.build_exhaustion(stories)
    stories = engine.build_signals(stories, as_of=T0 + timedelta(minutes=10))

    signal = stories[0].signals[0]
    assert signal.event_state == "exhausted"
    assert signal.signal_state == "EXHAUSTED"


def test_confirmed_transition_when_market_reaction_is_independently_corroborated() -> None:
    # Same event, but the reaction now carries independent corroboration
    # (elevated volume, VWAP, benchmark and sector alignment), so CP5 market
    # confirmation reaches "confirmed" and CP11 should report CONFIRMED
    # instead of a fresh EARLY_LONG.
    market_observations = [
        market_obs(-5, 1000.0, average_volume=1000.0),
        market_obs(
            5, 1006.0, volume=1500.0, average_volume=1000.0,
            vwap=1005.0, benchmark_return=0.001, sector_return=0.001,
        ),
    ]
    engine, stories = _run_pipeline(T0 + timedelta(hours=1), market_observations)
    signal = stories[0].signals[0]
    assert signal.signal_state == "CONFIRMED"


def test_delivery_payload_carries_signal_and_stays_priority_independent() -> None:
    market_observations = [market_obs(-5, 1000.0), market_obs(5, 1006.0)]
    engine, stories = _run_pipeline(T0 + timedelta(hours=1), market_observations)
    ranked = engine.rank_prioritization(stories)
    payload = build_intelligence_payload(stories, ranked, generated_at=T0 + timedelta(hours=1))

    assert payload["summary"]["signal_count"] == 1
    ranked_entry = payload["ranked_events"][0]
    assert ranked_entry["signal"]["signal_state"] == "EARLY_LONG"
    assert ranked_entry["signal"]["signal_strength_or_confidence"] != ranked_entry["priority"]["priority_score"]
    assert "signals" in payload["stories"][0]


def test_verified_issuer_alias_is_wired_into_entity_resolution() -> None:
    builder = IssuerMasterBuilder(INSTRUMENTS)
    records = builder.merge(
        [{"symbol": "RELIANCE", "company_name": "Reliance Industries Limited", "verified": "true"}]
    )
    engine = StoryEngine(INSTRUMENTS, issuer_records=records)
    observations = [observation("Reliance Industries Limited wins order worth INR 500 crore")]
    stories = engine.build_stories(observations)

    assert stories[0].semantic_events[0].instruments == ("RELIANCE",)


def test_unverified_issuer_alias_is_never_used_for_resolution() -> None:
    builder = IssuerMasterBuilder(INSTRUMENTS)
    records = builder.merge(
        [{"symbol": "HDFCBANK", "company_name": "HDFC Bank Limited", "verified": "false"}]
    )
    engine = StoryEngine(INSTRUMENTS, issuer_records=records)
    observations = [observation("HDFC Bank Limited wins order worth INR 500 crore")]
    stories = engine.build_stories(observations)

    assert stories[0].semantic_events[0].instruments == ()
