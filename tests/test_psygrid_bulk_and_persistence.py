"""PSYGRID bulk ingestion, restart recovery, outcome memory and daily diagnostics."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from live_support import Bars, at, background_universe, make_engine, nse_filing, snapshot, state_of

from psygridevents.market_snapshot import PsygridBulkClient
from psygridevents.storage import Store

IST = ZoneInfo("Asia/Kolkata")
FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "psygrid_stock_response_sample.json").read_text())


def _stock(symbol: str, minutes: int, start_price: float = 100.0, *, bad: bool = False, future: bool = False) -> dict:
    candles = []
    price = start_price
    for minute in range(minutes):
        stamp = (at(minute)).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        candles.append({"timestamp": stamp, "open": price, "high": price * 1.001, "low": price * 0.999,
                        "close": price, "volume": 1000})
        price *= 1.0005
    if bad:
        candles.append({"timestamp": "garbage", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})
        candles.append({"timestamp": candles[0]["timestamp"], "open": -5, "high": 1, "low": 1, "close": 1, "volume": 1})
    if future:
        stamp = (at(minutes + 120)).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        candles.append({"timestamp": stamp, "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1})
    return {"symbol": symbol, "security_id": "1", "previous_close": start_price * 0.99, "today_open": start_price,
            "candles_1m": candles}


def _transport(*, status: str = "OK", fail_shards: set[str] = frozenset(), minutes: int = 30, all_fail: bool = False):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if all_fail:
            raise httpx.ConnectError("psygrid down")
        if path.startswith("/public/live-"):
            letter = path.split("-")[1].split(".")[0]
            if letter in fail_shards:
                return httpx.Response(503)
            stocks = {}
            if letter == "a":
                stocks = {"RELIANCE": _stock("RELIANCE", minutes, 2900, bad=True, future=True),
                          "TCS": _stock("TCS", minutes, 4000)}
            elif letter == "b":
                stocks = {f"S{index}": _stock(f"S{index}", minutes, 50 + index) for index in range(8)}
            return httpx.Response(200, json={"service": "PSYGRID", "schema_version": "4.0", "status": status,
                                             "session": {"status": "LIVE" if status == "OK" else status,
                                                         "current_time_ist": "2026-10-05 09:46:10 IST"},
                                             "stocks": stocks})
        if path == "/public/nifty.json":
            return httpx.Response(200, json={"service": "PSYGRID", "1m": [
                {"timestamp": at(m).astimezone(IST).strftime("%Y-%m-%d %H:%M:%S IST"), "open": 1, "high": 1, "low": 1,
                 "close": 25000 + m, "volume": 0} for m in range(minutes)]})
        if path == "/public/indiavix.json":
            return httpx.Response(200, json={"service": "PSYGRID", "ltp": 12.9})
        if path == "/public/sectors.json":
            return httpx.Response(200, json={"service": "PSYGRID", "sectors": [
                {"sector": "INFORMATION_TECHNOLOGY", "constituents": [{"symbol": "TCS"}]}]})
        if path == "/health":
            return httpx.Response(200, json={"status": "OK"})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def _snapshot(**kwargs):
    async def go():
        client = PsygridBulkClient("http://psygrid.invalid", transport=_transport(**kwargs))
        try:
            return await client.snapshot(as_of=at(30) + timedelta(seconds=20))
        finally:
            await client.aclose()

    return asyncio.run(go())


def test_bulk_snapshot_reads_shards_not_per_stock_and_rejects_bad_candles():
    calls = []
    base = _transport()

    def counting(request):
        calls.append(request.url.path)
        return base.handle_request(request)

    async def go():
        client = PsygridBulkClient("http://psygrid.invalid", transport=httpx.MockTransport(counting))
        try:
            return await client.snapshot(as_of=at(30) + timedelta(seconds=20))
        finally:
            await client.aclose()

    snap = asyncio.run(go())
    assert not any("/public/stock/" in path for path in calls)
    assert sum(path.startswith("/public/live-") for path in calls) == 22
    reliance = snap.series["RELIANCE"]
    assert reliance.malformed == 2 and reliance.future_rejected == 1
    assert len(reliance) == 30
    assert snap.health() == "HEALTHY"
    assert snap.symbol_freshness("TCS") == "LIVE"
    assert snap.vix == 12.9 and snap.benchmark is not None and snap.sector_of["TCS"] == "INFORMATION_TECHNOLOGY"
    observations = snap.observations("TCS", anchor_epoch=at(10).timestamp())
    assert observations[-1].vwap is not None and observations[-1].average_volume is not None


def test_psygrid_outage_and_partial_outage_states():
    assert _snapshot(all_fail=True).health() == "DISCONNECTED"
    assert _snapshot(fail_shards={"c", "d"}).health() == "DEGRADED"
    assert _snapshot(status="CLOSED").health() == "CLOSED"
    assert _snapshot(status="AUTH_ERROR").health() == "ERROR"


def test_stale_feed_is_detected_from_bar_age():
    snap = _snapshot(minutes=10)  # last bar 20 minutes before as_of
    assert snap.symbol_freshness("TCS") == "STALE"
    assert snap.health() == "STALE"


def test_fixture_from_psygrid_code_parses():
    from datetime import datetime, timezone

    from psygridevents.market_snapshot import parse_stock_payload

    series = parse_stock_payload("RELIANCE", FIXTURE, as_of=datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert len(series) == 3 and series.previous_close == 2900.0


# --------------------------------------------------------------------------- restart recovery
def test_restart_recovers_state_without_reannouncing(tmp_path):
    db = tmp_path / "svc.sqlite3"
    engine = make_engine(tmp_path, store=Store(db))
    when = at(60)
    engine.ingest([nse_filing("LT", "Larsen & Toubro Limited", "Bagging of Order", when,
                              summary="Larsen & Toubro bags order worth Rs 2,500 crore")], when + timedelta(seconds=20))
    lt = Bars("LT", 3500).move(60, 160, 0.0012, volume_mult=3.0)
    bars = [lt] + background_universe(engine, exclude={"LT"})
    alerts_before = []
    for minute in (62, 64, 66, 70):
        now = at(minute) + timedelta(seconds=10)
        alerts_before += engine.evaluate(snapshot(now, bars), now).alerts
    state_before = state_of(engine, "LT")
    assert state_before == "CONFIRMED" and alerts_before
    engine.store.close()

    # ---- process restarts ----
    restarted = make_engine(tmp_path, store=Store(db))
    counts = restarted.restore(at(71))
    assert counts["stories"] == 1 and counts["signals"] >= 1 and counts["open_outcomes"] >= 1
    assert state_of(restarted, "LT") == state_before
    # The same observation re-polled after restart is a duplicate, not a new story.
    restarted.ingest([nse_filing("LT", "Larsen & Toubro Limited", "Bagging of Order", when,
                                 summary="Larsen & Toubro bags order worth Rs 2,500 crore")], at(72))
    assert restarted.store.counts()["stories"] == 1
    now = at(72) + timedelta(seconds=10)
    result = restarted.evaluate(snapshot(now, bars), now)
    assert not [item for item in result.alerts if item.symbol == "LT"]
    assert restarted.top and restarted.top[0].symbol == "LT"


def test_outcomes_are_recorded_and_completed(tmp_path):
    engine = make_engine(tmp_path)
    when = at(60)
    engine.ingest([nse_filing("LT", "Larsen & Toubro Limited", "Bagging of Order", when,
                              summary="Larsen & Toubro bags order worth Rs 2,500 crore")], when + timedelta(seconds=20))
    lt = Bars("LT", 3500).move(60, 200, 0.0008, volume_mult=3.0)
    bars = [lt] + background_universe(engine, exclude={"LT"})
    for minute in list(range(61, 80)) + list(range(80, 140, 5)):
        now = at(minute) + timedelta(seconds=10)
        engine.evaluate(snapshot(now, bars), now)
    outcomes = engine.store.outcomes()
    signal_outcomes = [item for item in outcomes if item["reference_kind"] == "signal_actionable"]
    event_outcomes = [item for item in outcomes if item["reference_kind"] == "event_first_live_evaluation"]
    assert signal_outcomes and event_outcomes
    done = signal_outcomes[0]
    assert done["completed"] is True
    assert set(done["returns"]) >= {"1m", "5m", "15m", "30m", "60m"}
    assert done["returns"]["60m"] > 0 and done["mfe"] >= done["returns"]["60m"] and done["mae"] <= 0
    assert done["time_to_peak_minutes"] is not None
    stats = engine.store.outcome_statistics()
    assert stats["buckets"] and all(not bucket["sufficient_sample"] for bucket in stats["buckets"])
    assert "Not probabilities" in stats["note"]


def test_end_of_day_saves_profiles_and_diagnostics(tmp_path):
    engine = make_engine(tmp_path)
    when = at(60)
    engine.ingest([nse_filing("LT", "Larsen & Toubro Limited", "Bagging of Order", when,
                              summary="Larsen & Toubro bags order worth Rs 2,500 crore")], when + timedelta(seconds=20))
    bars = [Bars("LT", 3500).move(60, 200, 0.0008, volume_mult=3.0)] + background_universe(engine, exclude={"LT"})
    for minute in (62, 66, 70, 90):
        now = at(minute) + timedelta(seconds=10)
        engine.evaluate(snapshot(now, bars), now)
    close = at(376)
    diagnostics = engine.end_of_day(snapshot(close, bars), close)
    assert diagnostics["actionable_signals"] >= 1
    assert engine.store.daily_diagnostics()[0]["trade_date"] == "2026-10-05"
    profiles = engine.store.volume_profiles(before_date="2026-10-06")
    assert "LT" in profiles and profiles["LT"][0]
    assert all(item["completed"] for item in engine.store.outcomes())
