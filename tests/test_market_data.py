from datetime import datetime, timedelta, timezone

from psygridevents.market_confirmation import MarketObservation
from psygridevents.market_data import NullMarketDataAdapter, StaticMarketDataAdapter

T0 = datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc)


def obs(minutes: int, close: float) -> MarketObservation:
    return MarketObservation(
        symbol="RELIANCE", timestamp=T0 + timedelta(minutes=minutes),
        open=close, high=close, low=close, close=close, volume=1000.0,
    )


def test_null_adapter_never_fabricates_an_observation() -> None:
    adapter = NullMarketDataAdapter()
    assert adapter.observations("RELIANCE", as_of=T0) == ()


def test_static_adapter_serves_only_matching_symbol() -> None:
    adapter = StaticMarketDataAdapter([obs(-5, 100.0), obs(5, 101.0)])
    assert adapter.observations("HDFCBANK", as_of=T0 + timedelta(hours=1)) == ()
    assert len(adapter.observations("RELIANCE", as_of=T0 + timedelta(hours=1))) == 2


def test_static_adapter_enforces_the_real_time_boundary() -> None:
    adapter = StaticMarketDataAdapter([obs(-5, 100.0), obs(5, 101.0), obs(60, 110.0)])
    result = adapter.observations("RELIANCE", as_of=T0 + timedelta(minutes=10))
    assert [item.timestamp for item in result] == [T0 + timedelta(minutes=-5), T0 + timedelta(minutes=5)]
