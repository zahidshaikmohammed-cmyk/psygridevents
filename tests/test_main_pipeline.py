import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from psygridevents.acquisition import RawObservation
from psygridevents.main import build_market_data_adapter, run_pipeline
from psygridevents.market_confirmation import MarketObservation
from psygridevents.market_data import NullMarketDataAdapter, PsygridMarketDataAdapter, StaticMarketDataAdapter
from psygridevents.story_engine import StoryEngine

ROOT = Path(__file__).resolve().parents[1]
INSTRUMENTS = ROOT / "config" / "instruments.json"
T0 = datetime(2026, 9, 19, 9, 30, tzinfo=timezone.utc)


def _args(**overrides) -> argparse.Namespace:
    values = dict(market_data="psygrid", market_data_url="http://example.test:10000")
    values.update(overrides)
    return argparse.Namespace(**values)


def test_build_market_data_adapter_none_is_null_adapter() -> None:
    adapter = build_market_data_adapter(_args(market_data="none"))
    assert isinstance(adapter, NullMarketDataAdapter)


def test_build_market_data_adapter_psygrid_uses_configured_url() -> None:
    adapter = build_market_data_adapter(_args(market_data="psygrid", market_data_url="http://example.test:10000"))
    assert isinstance(adapter, PsygridMarketDataAdapter)


def _observation(title: str) -> RawObservation:
    return RawObservation(
        provider_id="test", source_tier=0, publisher="Official Source", title=title,
        url="https://example.com/main-pipeline", summary=title, published_at=T0, observed_at=T0, raw={},
    )


def test_run_pipeline_end_to_end_with_offline_market_data() -> None:
    engine = StoryEngine(INSTRUMENTS)
    with mock.patch.object(StoryEngine, "acquire", return_value=[_observation("RELIANCE wins order worth INR 500 crore")]):
        observations, stories, ranked, payload = run_pipeline(
            engine, feeds=[], market_data=NullMarketDataAdapter(), as_of=T0
        )

    assert len(observations) == 1
    assert len(stories) == 1
    assert len(ranked) == 1
    signal = stories[0].signals[0]
    assert signal.asset == "RELIANCE"
    assert signal.signal_state == "WATCH"  # no market data supplied
    assert payload["summary"]["signal_count"] == 1


def test_run_pipeline_produces_early_long_with_synthetic_market_data() -> None:
    engine = StoryEngine(INSTRUMENTS)
    market_observations = [
        MarketObservation(symbol="RELIANCE", timestamp=T0 - timedelta(minutes=5), open=1000.0, high=1000.0, low=1000.0, close=1000.0, volume=1000.0),
        MarketObservation(symbol="RELIANCE", timestamp=T0 + timedelta(minutes=8), open=1006.0, high=1007.0, low=1005.0, close=1006.0, volume=1050.0),
    ]
    market_data = StaticMarketDataAdapter(market_observations)
    with mock.patch.object(StoryEngine, "acquire", return_value=[_observation("RELIANCE wins order worth INR 500 crore")]):
        _observations, stories, _ranked, payload = run_pipeline(
            engine, feeds=[], market_data=market_data, as_of=T0 + timedelta(hours=1)
        )

    signal = stories[0].signals[0]
    assert signal.signal_state == "EARLY_LONG"
    assert payload["ranked_events"][0]["signal"]["signal_state"] == "EARLY_LONG"


def test_run_pipeline_respects_since_for_incremental_acquisition() -> None:
    engine = StoryEngine(INSTRUMENTS)
    captured_since = []

    def fake_acquire(self, feeds, since=None):
        captured_since.append(since)
        return []

    with mock.patch.object(StoryEngine, "acquire", fake_acquire):
        run_pipeline(engine, feeds=[], market_data=NullMarketDataAdapter(), since=T0, as_of=T0 + timedelta(minutes=1))

    assert captured_since == [T0]
