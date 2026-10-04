"""Complete deterministic pipeline: raw feed bytes + raw PSYGRID JSON -> ranked TOP signals."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
from live_support import Bars, at, make_engine

from psygridevents.alerts import format_signal
from psygridevents.market_snapshot import PsygridBulkClient
from psygridevents.providers import SourceHttpClient, SourceQuality, SourceRegistry, SourceSpec

IST = ZoneInfo("Asia/Kolkata")


def _feed(items):
    body = "".join(
        f"<item><title>{t}</title><link>{link}</link><pubDate>{d}</pubDate><description>{desc}</description></item>"
        for t, link, d, desc in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'.encode()


def _rfc(dt):
    return dt.strftime("%a, %d %b %Y %H:%M:%S GMT")


def _psygrid_payloads(bars_by_symbol: dict[str, Bars], upto):
    stocks = {}
    for symbol, bars in bars_by_symbol.items():
        series = bars.series(upto)
        stocks[symbol] = {
            "symbol": symbol, "security_id": "1", "previous_close": series.previous_close, "today_open": series.today_open,
            "candles_1m": [
                {"timestamp": __import__("datetime").datetime.fromtimestamp(series.ts[i], tz=IST).strftime("%Y-%m-%d %H:%M:%S IST"),
                 "open": series.open[i], "high": series.high[i], "low": series.low[i], "close": series.close[i],
                 "volume": int(series.volume[i])}
                for i in range(len(series))
            ],
        }
    return stocks


def test_full_pipeline_from_raw_sources_and_raw_psygrid_to_top_signal():
    engine = make_engine()
    event_time = at(75)  # 10:30 IST
    lt_link = f"https://nsearchives.nseindia.com/corporate/LT_{event_time.astimezone(IST).strftime('%d%m%Y%H%M%S')}_BaggingofOrder.pdf"
    nse = _feed([
        ("Larsen &amp; Toubro Limited", lt_link, _rfc(event_time),
         "Larsen &amp; Toubro Limited has informed the Exchange regarding bagging of order worth Rs 2,500 crore"),
        ("Infosys Limited", "https://nsearchives.nseindia.com/corporate/INFY_05102026100000_ClosureofTradingWindow.pdf",
         _rfc(at(45)), "Closure of Trading Window"),
    ])
    media = _feed([
        ("L&amp;T bags Rs 2,500 crore NHAI order", "https://media.invalid/lt", _rfc(event_time + timedelta(minutes=4)),
         "Larsen &amp; Toubro bags Rs 2,500 crore order"),
        ("Brent crude surges 5% on supply worries", "https://media.invalid/crude", _rfc(event_time), "Oil prices jump"),
    ])
    registry = SourceRegistry([
        SourceSpec("nse", "NSE Corporate Announcements", "rss", SourceQuality.PRIMARY, "corporate_disclosure",
                   url="https://feeds.invalid/nse.xml"),
        SourceSpec("media", "Economic Times", "rss", SourceQuality.REPUTABLE_SECONDARY, "media",
                   url="https://feeds.invalid/media.xml"),
        SourceSpec("down", "Down", "rss", SourceQuality.REPUTABLE_SECONDARY, "media", url="https://down.invalid/x.xml"),
    ])

    def handler(request):
        if request.url.host == "down.invalid" and request.url.path != "/robots.txt":
            return httpx.Response(503)
        return {"/nse.xml": httpx.Response(200, content=nse), "/media.xml": httpx.Response(200, content=media)}.get(
            request.url.path, httpx.Response(404))

    http = SourceHttpClient(user_agent="e2e", transport=httpx.MockTransport(handler))
    bars = {"LT": Bars("LT", 3500).move(75, 200, 0.0011, volume_mult=3.5),
            "ONGC": Bars("ONGC", 250), "BPCL": Bars("BPCL", 300)}
    for index, symbol in enumerate(engine.universe[:80]):
        bars.setdefault(symbol, Bars(symbol, 100 + index))

    async def run_day():
        observed = event_time + timedelta(minutes=5)
        observations = []
        for item in registry.sources.values():
            if await registry.probe(item, http, observed):
                result = await registry.poll(item, http, since=None, now=observed, interval_seconds=60)
                observations += result.observations if result else []
        await http.aclose()
        engine.ingest(observations, observed)
        results = []
        for minute in (81, 84, 88, 95):
            now = at(minute) + timedelta(seconds=15)
            stocks = _psygrid_payloads(bars, now)
            letters = "abcdefghijklmnopqrstuv"

            def psygrid(request, stocks=stocks, letters=letters):
                path = request.url.path
                if path.startswith("/public/live-"):
                    letter = path.split("-")[1][0]
                    shard = {k: v for i, (k, v) in enumerate(sorted(stocks.items())) if letters[i % 22] == letter}
                    return httpx.Response(200, json={"service": "PSYGRID", "status": "OK",
                                                     "session": {"status": "LIVE"}, "stocks": shard})
                return httpx.Response(404)

            client = PsygridBulkClient("http://psygrid.invalid", transport=httpx.MockTransport(psygrid))
            snap = await client.snapshot(as_of=now, universe=engine.universe)
            await client.aclose()
            results.append(engine.evaluate(snap, now))
        return results

    results = asyncio.run(run_day())
    statuses = {key: item.tracker.status.state for key, item in registry.sources.items()}
    assert statuses["down"] == "DISCONNECTED" and statuses["nse"] == "HEALTHY"

    events = engine.store.events()
    lt_events = [event for event in events if event["symbols"] == ["LT"] and event["subtype"] == "order_win"]
    assert len(lt_events) == 1  # NSE filing + media report fused into one canonical event
    assert lt_events[0]["confirmation_status"] == "CONFIRMED_PRIMARY"
    infy = [event for event in events if event["symbols"] == ["INFY"]]
    assert infy and infy[0]["subtype"] == "routine_disclosure"

    final = results[-1]
    assert final.market_health == "HEALTHY"
    assert final.top and final.top[0].symbol == "LT"
    top = final.top[0].payload
    assert top["signal_state"] == "CONFIRMED" and top["direction"] == "LONG"
    assert top["event_time_basis"] == "nse_dissemination_timestamp"
    assert top["market_reaction"]["move_since_event"] > 0.005
    text = format_signal(top)
    assert "SYMBOL: LT" in text and "WHY NOW:" in text
    # Indirect crude exposures with no confirming price move never reach the TOP list.
    assert all(entry.symbol not in ("ONGC", "BPCL") for entry in final.top)
