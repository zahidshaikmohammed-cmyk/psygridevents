import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from psygridevents.market_confirmation import MarketObservation
from psygridevents.market_data import NullMarketDataAdapter, PsygridMarketDataAdapter, StaticMarketDataAdapter
from psygridevents.psygrid_client import PsygridClient

T0 = datetime(2026, 9, 17, 9, 30, tzinfo=timezone.utc)
ROOT = Path(__file__).resolve().parents[1]
STOCK_OK = json.loads((ROOT / "tests" / "fixtures" / "psygrid_stock_response_sample.json").read_text())


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


def _psygrid_adapter(handler) -> PsygridMarketDataAdapter:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="http://psygrid.test")
    return PsygridMarketDataAdapter("http://psygrid.test", client=PsygridClient("http://psygrid.test", client=http_client))


def test_psygrid_adapter_observations_reuses_psygrid_processed_feed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/public/stock/RELIANCE.json"
        return httpx.Response(200, json=STOCK_OK)

    adapter = _psygrid_adapter(handler)
    as_of = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    observations = adapter.observations("RELIANCE", as_of=as_of)

    assert len(observations) == 3
    assert all(o.symbol == "RELIANCE" for o in observations)
    # Fields Psygrid does not provide in this contract stay unavailable, never fabricated.
    assert all(o.vwap is None and o.average_volume is None for o in observations)
    assert all(o.benchmark_return is None and o.sector_return is None for o in observations)


def test_psygrid_adapter_fails_closed_on_outage_like_null_adapter() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = _psygrid_adapter(handler)
    assert adapter.observations("RELIANCE", as_of=T0) == ()


def test_psygrid_adapter_freshness_reports_live_when_recent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=STOCK_OK)

    adapter = _psygrid_adapter(handler)
    latest_ts = datetime(2025, 9, 19, 8, 27, tzinfo=timezone.utc)  # matches fixture's last candle in UTC
    freshness = adapter.freshness("RELIANCE", as_of=latest_ts + timedelta(seconds=30), max_age_seconds=120)

    assert freshness.status == "LIVE"
    assert freshness.data_age_seconds == 30.0


def test_psygrid_adapter_freshness_reports_stale_when_old() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=STOCK_OK)

    adapter = _psygrid_adapter(handler)
    latest_ts = datetime(2025, 9, 19, 8, 27, tzinfo=timezone.utc)
    freshness = adapter.freshness("RELIANCE", as_of=latest_ts + timedelta(minutes=10), max_age_seconds=120)

    assert freshness.status == "STALE"


def test_psygrid_adapter_freshness_reports_no_data_for_empty_symbol() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"service": "PSYGRID", "symbol": "GHOST", "status": "NOT_FOUND"})

    adapter = _psygrid_adapter(handler)
    freshness = adapter.freshness("GHOST", as_of=T0)
    assert freshness.status in ("NO_DATA", "UNAVAILABLE")


def test_psygrid_adapter_real_time_boundary_rejects_future_observation() -> None:
    payload = json.loads(json.dumps(STOCK_OK))
    payload["candles_1m"].append(dict(payload["candles_1m"][-1], timestamp="2099-01-01 00:00:00 IST"))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    adapter = _psygrid_adapter(handler)
    as_of = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
    observations = adapter.observations("RELIANCE", as_of=as_of)

    assert all(o.timestamp <= as_of for o in observations)
    assert len(observations) == 3
