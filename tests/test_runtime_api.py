"""Service runtime + HTTP API + settings + calendar (mocked network, real server on an ephemeral port)."""
from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import replace
from datetime import datetime, timezone

import httpx
import pytest

from psygridevents.alerts import TelegramChannel, format_signal
from psygridevents.market_calendar import MarketCalendar, SessionPhase
from psygridevents.market_snapshot import PsygridBulkClient
from psygridevents.providers import SourceHttpClient
from psygridevents.runtime import ServiceRuntime
from psygridevents.settings import SessionSettings, TelegramSettings, load_settings

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>
<item><title>Larsen &amp; Toubro Limited</title>
<link>https://nsearchives.nseindia.com/corporate/LT_02102026101500_BaggingofOrder.pdf</link>
<pubDate>Fri, 02 Oct 2026 04:45:00 GMT</pubDate><description>Larsen &amp; Toubro bags order worth Rs 2,500 crore</description></item>
</channel></rss>"""


def _sources_file(tmp_path):
    path = tmp_path / "sources.yaml"
    path.write_text("""
version: test
sources:
  - {id: nse_test, name: NSE test, kind: rss, quality: PRIMARY, category: corporate_disclosure,
     url: "https://feeds.invalid/nse.xml", verification: documented_indexed, poll_seconds: 30}
  - {id: broken, name: Broken, kind: rss, quality: REPUTABLE_SECONDARY, category: media,
     url: "https://broken.invalid/x.xml", verification: curated_directory}
  - {id: none, name: Nothing, kind: unavailable, quality: OFFICIAL, category: x, verification: none, notes: "no feed"}
""", encoding="utf-8")
    return path


def _http():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "feeds.invalid" and request.url.path == "/nse.xml":
            return httpx.Response(200, content=RSS)
        if request.url.host == "broken.invalid" and request.url.path.endswith(".xml"):
            raise httpx.ConnectError("down")
        return httpx.Response(404)

    return SourceHttpClient(user_agent="test", transport=httpx.MockTransport(handler))


def _psygrid():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/public/live-"):
            return httpx.Response(200, json={"service": "PSYGRID", "status": "CLOSED", "session": {"status": "CLOSED"},
                                             "stocks": {}})
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "OK"})
        return httpx.Response(404)

    return PsygridBulkClient("http://psygrid.invalid", transport=httpx.MockTransport(handler))


def _get(port: int, path: str):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
        body = response.read().decode()
        return response.status, (json.loads(body) if response.headers["Content-Type"].startswith("application/json") else body)


def test_runtime_cycle_api_endpoints_and_graceful_shutdown(tmp_path):
    settings = replace(load_settings(env={}), data_dir=tmp_path, sources_file=_sources_file(tmp_path), api_port=0)
    runtime = ServiceRuntime(settings, http=_http(), market=_psygrid())

    async def scenario():
        now = datetime.now(timezone.utc)
        runtime.engine.restore(now)
        phase = runtime.phase(now)
        observations = []
        for item in runtime.registry.sources.values():
            observations += await runtime._run_source(item, now, phase)
        runtime.engine.ingest(observations, now)
        await runtime.market_cycle()
        return runtime.registry

    registry = asyncio.run(scenario())
    states = {key: item.tracker.status.state for key, item in registry.sources.items()}
    assert states == {"nse_test": "HEALTHY", "broken": "DISCONNECTED", "none": "UNAVAILABLE"}

    from psygridevents.api import ApiServer

    api = ApiServer("127.0.0.1", 0, runtime.published, runtime.store, runtime.liveness)
    api.start()
    port = api.httpd.server_address[1]
    try:
        for path in ("/health", "/providers", "/events", "/events/latest", "/stories", "/signals", "/signals/top",
                     "/signals/LT", "/market/health", "/system/status", "/metrics", "/outcomes", "/outcomes/stats",
                     "/diagnostics/daily", "/transitions"):
            status, body = _get(port, path)
            assert status == 200, path
            assert isinstance(body, dict), path
        status, body = _get(port, "/events")
        assert body["count"] >= 1 and body["events"][0]["symbols"] == ["LT"]
        status, body = _get(port, "/providers")
        assert {item["provider_id"] for item in body["providers"]} == {"nse_test", "broken", "none"}
        status, body = _get(port, "/signals/top")
        assert body["score_is_probability"] is False and body["signals"] == []
        status, body = _get(port, "/market/health")
        assert body["health"] == "CLOSED"
        status, text = _get(port, "/metrics?format=prometheus")
        assert "psygridevents_providers_total 3" in text
        status, html = _get(port, "/")
        assert "PSYGRID event signals" in html
        status, body = _get(port, "/system/status")
        assert body["settings"]["telegram"]["bot_token"] is None
        with pytest.raises(urllib.error.HTTPError):
            _get(port, "/nope")
    finally:
        api.stop()
    asyncio.run(runtime.shutdown())


def test_settings_env_overrides_and_redaction():
    settings = load_settings(env={
        "PSYGRID_BASE_URL": "http://127.0.0.1:10000/", "PSYGRIDEVENTS_API_PORT": "9999", "TELEGRAM_BOT_TOKEN": "secret",
        "TELEGRAM_CHAT_ID": "42", "ANTHROPIC_API_KEY": "sk-test", "PSYGRIDEVENTS_AI_ENABLED": "1",
        "PSYGRIDEVENTS_MIN_OPPORTUNITY_SCORE": "70", "PSYGRID_FETCH_MODE": "full",
    })
    assert settings.market.psygrid_base_url == "http://127.0.0.1:10000"
    assert settings.api_port == 9999 and settings.signals.min_opportunity_score == 70
    assert settings.telegram.enabled and settings.ai.available and settings.market.fetch_mode == "full"
    dumped = json.dumps(settings.safe_dict())
    assert "secret" not in dumped and "sk-test" not in dumped
    with pytest.raises(ValueError):
        load_settings(env={"PSYGRID_FETCH_MODE": "per_stock"})


def test_calendar_phases_holidays_and_event_relation(tmp_path):
    holidays = tmp_path / "cal.yaml"
    holidays.write_text("holidays:\n  - {date: 2026-10-20, name: test holiday}\n", encoding="utf-8")
    calendar = MarketCalendar.load(SessionSettings(), holidays)
    ist = lambda d, h, m: datetime(2026, 10, d, h, m, tzinfo=MarketCalendar.ist(datetime.now(timezone.utc)).tzinfo)  # noqa: E731
    assert calendar.phase(ist(5, 7, 0)) == SessionPhase.OVERNIGHT
    assert calendar.phase(ist(5, 8, 30)) == SessionPhase.PRE_MARKET
    assert calendar.phase(ist(5, 10, 0)) == SessionPhase.MARKET
    assert calendar.phase(ist(5, 15, 10)) == SessionPhase.NEAR_CLOSE
    assert calendar.phase(ist(5, 15, 45)) == SessionPhase.POST_MARKET
    assert calendar.phase(ist(4, 10, 0)) == SessionPhase.NON_TRADING_DAY  # Sunday
    assert calendar.phase(ist(20, 10, 0)) == SessionPhase.NON_TRADING_DAY  # configured holiday
    now = ist(5, 10, 0)
    assert calendar.event_session_relation(ist(5, 9, 50), now) == "DURING_SESSION"
    assert calendar.event_session_relation(ist(5, 8, 0), now) == "BEFORE_OPEN"
    assert calendar.event_session_relation(ist(2, 18, 0), now) == "BEFORE_OPEN"  # Friday evening -> Monday gap
    assert calendar.event_session_relation(ist(2, 11, 0), now) == "PRIOR_SESSION"
    assert calendar.event_session_relation(ist(5, 16, 0), ist(5, 16, 5)) == "AFTER_CLOSE"


def test_signal_text_format_and_telegram_failure_is_isolated():
    payload = {"rank": 1, "symbol": "XYZ", "direction": "LONG", "signal_state": "CONFIRMED", "opportunity_score": 94.2,
               "event": {"headline": "Order win", "magnitude_crore": 800, "subtype": "order_win"},
               "freshness": {"event_age": "4m 21s", "freshness_status": "FRESH"},
               "market_reaction": {"move_since_event": 0.034}, "price": 101.2, "intraday_return": 0.03,
               "relative_strength": {"label": "Strong", "value": 0.028, "basis": "index:nifty"},
               "volume_confirmation": {"ratio": 3.1}, "VWAP_state": "above", "exhaustion_state": "LOW",
               "why_now": "Fresh material event + immediate price confirmation", "invalidation": "Below 98.00",
               "source": {"best_quality": "PRIMARY", "publishers": ["nse"]}, "source_urls": ["https://x"],
               "risk_flags": []}
    text = format_signal(payload)
    for line in ("PSYGRID EVENT SIGNAL", "RANK #1", "SYMBOL: XYZ", "DIRECTION: LONG", "STATE: CONFIRMED",
                 "OPPORTUNITY SCORE: 94.2", "₹800 Cr", "4m 21s", "+3.40%", "Strong", "3.1x baseline", "Above",
                 "WHY NOW:", "INVALIDATION:", "SOURCE:"):
        assert line in text, line

    class T:
        symbol, from_state, to_state = "XYZ", "WATCH", "CONFIRMED"

    channel = TelegramChannel(TelegramSettings(bot_token="t", chat_id="c", min_interval_seconds=0),
                              transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    async def go():
        await channel.deliver(T(), payload)
        await channel.aclose()

    asyncio.run(go())
    assert channel.failures == 1 and channel.sent == 0


def test_optional_api_token_protects_everything_but_health(tmp_path):
    from psygridevents.api import ApiServer, Published
    from psygridevents.storage import Store

    api = ApiServer("127.0.0.1", 0, Published(), Store(tmp_path / "x.sqlite3"),
                    lambda: {"status": "OK"}, token="s3cret")
    api.start()
    port = api.httpd.server_address[1]
    try:
        assert _get(port, "/health")[0] == 200
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            _get(port, "/signals/top")
        assert excinfo.value.code == 401
        assert _get(port, "/signals/top?token=s3cret")[0] == 200
    finally:
        api.stop()
