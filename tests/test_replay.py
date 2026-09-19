from datetime import datetime, timedelta, timezone
from pathlib import Path

from psygridevents.acquisition import RawObservation
from psygridevents.market_confirmation import MarketObservation
from psygridevents.replay import replay
from psygridevents.story_engine import StoryEngine

ROOT = Path(__file__).resolve().parents[1]
INSTRUMENTS = ROOT / "config" / "instruments.json"

EVENT_TIME = datetime(2026, 9, 21, 10, 1, tzinfo=timezone.utc)  # a Monday


def _observation() -> RawObservation:
    return RawObservation(
        provider_id="test", source_tier=0, publisher="Official Source",
        title="RELIANCE wins order worth INR 500 crore", url="https://example.com/replay-1",
        summary="RELIANCE wins order worth INR 500 crore",
        published_at=EVENT_TIME, observed_at=EVENT_TIME, raw={},
    )


def test_no_future_observation_is_ever_visible_to_an_earlier_checkpoint() -> None:
    # Exactly the example from the mission: event at 10:01, a checkpoint at
    # 10:02 may only see observations <= 10:02, and must never use the 10:05
    # candle to decide the 10:02 state.
    checkpoint_10_02 = EVENT_TIME + timedelta(minutes=1)  # event is at 10:01, so this is 10:02
    market_observations = [
        MarketObservation(symbol="RELIANCE", timestamp=EVENT_TIME - timedelta(minutes=1), open=1000.0, high=1000.0, low=1000.0, close=1000.0, volume=1000.0),  # 10:00
        MarketObservation(symbol="RELIANCE", timestamp=checkpoint_10_02, open=1003.0, high=1003.0, low=1003.0, close=1003.0, volume=1000.0),  # exactly 10:02
        MarketObservation(symbol="RELIANCE", timestamp=EVENT_TIME + timedelta(minutes=4), open=1050.0, high=1050.0, low=1050.0, close=1050.0, volume=5000.0),  # 10:05
    ]
    engine = StoryEngine(INSTRUMENTS)
    checkpoints = replay(engine, [_observation()], market_observations, [checkpoint_10_02])

    assert len(checkpoints) == 1
    result = checkpoints[0]
    # The 10:00 baseline and the 10:02 candle are visible; the 10:05 candle
    # must never be used to decide the 10:02 state.
    assert result.market_observations_used == 2
    assert result.latest_observation_timestamp_used == checkpoint_10_02
    assert result.latest_observation_timestamp_used < EVENT_TIME + timedelta(minutes=4)


def test_signal_evolves_across_checkpoints_without_leaking_the_future() -> None:
    baseline = MarketObservation(symbol="RELIANCE", timestamp=EVENT_TIME - timedelta(minutes=5), open=1000.0, high=1000.0, low=1000.0, close=1000.0, volume=1000.0)
    early = MarketObservation(symbol="RELIANCE", timestamp=EVENT_TIME + timedelta(minutes=8), open=1005.0, high=1005.0, low=1005.0, close=1005.0, volume=1000.0)
    later = MarketObservation(symbol="RELIANCE", timestamp=EVENT_TIME + timedelta(minutes=40), open=1006.0, high=1006.0, low=1006.0, close=1006.0, volume=1000.0)
    market_observations = [baseline, early, later]

    engine = StoryEngine(INSTRUMENTS)
    checkpoints = replay(
        engine, [_observation()], market_observations,
        [EVENT_TIME - timedelta(minutes=1), EVENT_TIME + timedelta(minutes=10), EVENT_TIME + timedelta(minutes=45)],
    )

    assert len(checkpoints) == 3
    before, mid, after = checkpoints

    # Before the event resolves any reaction: baseline visible, no reaction yet.
    assert before.market_observations_used == 1
    assert not any(state == "EARLY_LONG" for state in before.signal_states.values())

    # Mid checkpoint sees baseline + the early candle only, never `later`.
    assert mid.market_observations_used == 2
    assert mid.latest_observation_timestamp_used == early.timestamp
    assert any(state == "EARLY_LONG" for state in mid.signal_states.values())

    # After checkpoint sees all three, including `later`.
    assert after.market_observations_used == 3
    assert after.latest_observation_timestamp_used == later.timestamp


def test_replay_is_deterministic() -> None:
    market_observations = [
        MarketObservation(symbol="RELIANCE", timestamp=EVENT_TIME - timedelta(minutes=5), open=1000.0, high=1000.0, low=1000.0, close=1000.0, volume=1000.0),
        MarketObservation(symbol="RELIANCE", timestamp=EVENT_TIME + timedelta(minutes=8), open=1005.0, high=1005.0, low=1005.0, close=1005.0, volume=1000.0),
    ]
    checkpoint_at = EVENT_TIME + timedelta(minutes=10)

    engine_a = StoryEngine(INSTRUMENTS)
    engine_b = StoryEngine(INSTRUMENTS)
    result_a = replay(engine_a, [_observation()], market_observations, [checkpoint_at])
    result_b = replay(engine_b, [_observation()], market_observations, [checkpoint_at])

    assert result_a[0].signal_states == result_b[0].signal_states
    assert result_a[0].market_observations_used == result_b[0].market_observations_used


def test_replay_with_no_market_observations_yields_watch_not_a_guess() -> None:
    engine = StoryEngine(INSTRUMENTS)
    checkpoints = replay(engine, [_observation()], [], [EVENT_TIME + timedelta(minutes=5)])
    signal_states = checkpoints[0].signal_states
    assert signal_states  # at least one event resolved
    assert all(state in ("WATCH", "NO_SIGNAL") for state in signal_states.values())
