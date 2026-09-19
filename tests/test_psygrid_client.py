import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from psygridevents.psygrid_client import PsygridClient, parse_ist_timestamp

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"

# Generated for real from Psygrid's own state.py/output.py (see
# docs/LIVE_MARKET_DATA_INTEGRATION.md for the exact generation steps), not
# hand-typed, so these fixtures prove the parser matches Psygrid's actual
# serialization rather than an assumption about its shape.
STOCK_OK = json.loads((FIXTURES / "psygrid_stock_response_sample.json").read_text())
STOCK_NOT_FOUND = json.loads((FIXTURES / "psygrid_stock_not_found_sample.json").read_text())

AS_OF = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)


def _client(handler) -> PsygridClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="http://psygrid.test")
    return PsygridClient("http://psygrid.test", client=http_client)


def test_parse_ist_timestamp_converts_to_utc() -> None:
    assert parse_ist_timestamp("2026-09-19 09:31:00 IST") == datetime(2026, 9, 19, 4, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize("value", [None, "", "not a timestamp", 12345, "2026-13-40 99:99:99 IST"])
def test_parse_ist_timestamp_malformed_returns_none(value) -> None:
    assert parse_ist_timestamp(value) is None


def test_fetch_stock_observations_parses_the_real_psygrid_fixture() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/public/stock/RELIANCE.json"
        return httpx.Response(200, json=STOCK_OK)

    client = _client(handler)
    observations, diagnostics = client.fetch_stock_observations("RELIANCE", as_of=AS_OF)

    assert len(observations) == 3
    assert diagnostics.malformed_count == 0
    assert diagnostics.future_rejected_count == 0
    assert diagnostics.error is None
    first = observations[0]
    assert first.symbol == "RELIANCE"
    assert first.open == 2905.0
    assert first.close == 2905.5
    assert first.volume == 1000
    assert [o.timestamp for o in observations] == sorted(o.timestamp for o in observations)


def test_not_found_symbol_yields_no_observations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=STOCK_NOT_FOUND)

    client = _client(handler)
    observations, diagnostics = client.fetch_stock_observations("NOSUCHSYMBOL", as_of=AS_OF)

    assert observations == ()
    assert "not found" in diagnostics.error


def test_http_error_status_yields_no_observations_and_records_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"service": "PSYGRID", "status": "CONFIG_ERROR"})

    client = _client(handler)
    observations, diagnostics = client.fetch_stock_observations("RELIANCE", as_of=AS_OF)

    assert observations == ()
    assert diagnostics.http_status == 503
    assert "503" in diagnostics.error


def test_network_failure_yields_no_observations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = _client(handler)
    observations, diagnostics = client.fetch_stock_observations("RELIANCE", as_of=AS_OF)

    assert observations == ()
    assert diagnostics.http_status is None
    assert "ConnectError" in diagnostics.error


def test_malformed_json_yields_no_observations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json at all")

    client = _client(handler)
    observations, diagnostics = client.fetch_stock_observations("RELIANCE", as_of=AS_OF)

    assert observations == ()
    assert "invalid JSON" in diagnostics.error


def test_future_candle_relative_to_as_of_is_rejected() -> None:
    payload = json.loads(json.dumps(STOCK_OK))
    future = dict(payload["candles_1m"][-1])
    future["timestamp"] = "2099-01-01 09:31:00 IST"
    payload["candles_1m"].append(future)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _client(handler)
    observations, diagnostics = client.fetch_stock_observations("RELIANCE", as_of=AS_OF)

    assert len(observations) == 3  # the future candle is excluded
    assert diagnostics.future_rejected_count == 1
    assert all(o.timestamp <= AS_OF for o in observations)


@pytest.mark.parametrize(
    "corruption",
    [
        {"close": "not-a-number"},
        {"close": None},
        {"close": -5.0},
        {"timestamp": "garbage"},
        {"timestamp": None},
    ],
)
def test_malformed_candle_fields_are_dropped_not_fabricated(corruption) -> None:
    payload = json.loads(json.dumps(STOCK_OK))
    payload["candles_1m"][0].update(corruption)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _client(handler)
    observations, diagnostics = client.fetch_stock_observations("RELIANCE", as_of=AS_OF)

    assert len(observations) == 2
    assert diagnostics.malformed_count == 1


def test_universe_coverage_counts_resolved_live_stale_and_missing() -> None:
    live_payload = {
        "service": "PSYGRID",
        "schema_version": "4.0",
        "status": "OK",
        "session": {"current_time_ist": "2026-09-19 17:30:00 IST"},
        "universe_size": 3,
        "stock_count": 2,
        "stocks": {
            "RELIANCE": {
                "symbol": "RELIANCE", "security_id": "2885", "previous_close": 100.0, "today_open": 101.0,
                "candles_1m": [{"timestamp": "2026-09-19 17:29:00 IST", "open": 101, "high": 102, "low": 100, "close": 101.5, "volume": 10}],
            },
            "HDFCBANK": {
                "symbol": "HDFCBANK", "security_id": "1333", "previous_close": 100.0, "today_open": 101.0,
                "candles_1m": [{"timestamp": "2026-09-19 10:00:00 IST", "open": 101, "high": 102, "low": 100, "close": 101.5, "volume": 10}],
            },
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=live_payload)

    client = _client(handler)
    as_of = datetime(2026, 9, 19, 12, 0, 30, tzinfo=timezone.utc)  # 2026-09-19 17:30:30 IST
    report = client.universe_coverage(["RELIANCE", "HDFCBANK", "ICICIBANK"], as_of=as_of, max_age_seconds=120)

    assert report.configured == 3
    assert report.resolved_security_ids == 2
    assert report.live_data_received == 1  # RELIANCE: 90s old, within 120s
    assert report.stale == 1  # HDFCBANK: hours old
    assert report.missing == 1  # ICICIBANK not present at all
    assert report.error is None


def test_universe_coverage_fails_closed_when_endpoint_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = _client(handler)
    report = client.universe_coverage(["RELIANCE", "HDFCBANK"], as_of=AS_OF)

    assert report.configured == 2
    assert report.missing == 2
    assert report.error is not None
