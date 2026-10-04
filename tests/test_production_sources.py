"""Provider layer: probes, failure isolation, robots.txt, catalogue resolution, health states."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from psygridevents.providers import SourceHttpClient, SourceQuality, SourceRegistry, SourceSpec, load_source_specs
from psygridevents.providers.base import Activation
from psygridevents.providers.health import HealthState
from psygridevents.settings import CONFIG_DIR

NOW = datetime(2026, 10, 5, 4, 0, tzinfo=timezone.utc)


def rss(items: list[tuple[str, str, str]]) -> bytes:
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link><pubDate>{date}</pubDate><description>d</description></item>"
        for title, link, date in items
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'.encode()


GOOD = rss([("Larsen & Toubro Limited", "https://nsearchives.nseindia.com/corporate/LT_05102026093000_BaggingofOrder.pdf",
             "Mon, 05 Oct 2026 04:00:00 GMT"),
            ("Future item", "https://x.invalid/f", "Mon, 05 Oct 2026 23:00:00 GMT")])


def client(routes: dict[str, httpx.Response | Exception]) -> SourceHttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        for key, value in routes.items():
            if url == key or url.startswith(key):
                if isinstance(value, Exception):
                    raise value
                return value
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        return httpx.Response(404)

    return SourceHttpClient(user_agent="test-agent", transport=httpx.MockTransport(handler))


def spec(**kwargs) -> SourceSpec:
    base = dict(id="s1", name="S1", kind="rss", quality=SourceQuality.PRIMARY, category="c",
                url="https://good.invalid/feed.xml", activation=Activation.AUTO)
    base.update(kwargs)
    return SourceSpec(**base)


def run(coro):
    return asyncio.run(coro)


def test_production_source_matrix_loads_and_is_honest():
    specs = load_source_specs(CONFIG_DIR / "sources.yaml")
    ids = {item.id for item in specs}
    assert {"nse_announcements", "sebi_rss", "rbi_press_releases", "pib_releases", "gdelt_india_business",
            "google_news_india_markets", "bse_corporate_announcements"} <= ids
    for item in specs:
        assert item.verification in {"documented_indexed", "official_catalogue", "curated_directory", "none"}, item.id
        if item.kind == "unavailable":
            assert item.notes, f"{item.id} must explain why it is unavailable"
    qualities = {item.quality for item in specs}
    assert qualities == set(SourceQuality)


def test_one_failing_provider_does_not_stop_the_others():
    registry = SourceRegistry([spec(id="good"), spec(id="bad", url="https://bad.invalid/feed.xml")])
    http = client({"https://good.invalid/feed.xml": httpx.Response(200, content=GOOD),
                   "https://bad.invalid/feed.xml": httpx.ConnectError("boom")})

    async def go():
        results = {}
        for item in registry.sources.values():
            ok = await registry.probe(item, http, NOW)
            results[item.spec.id] = ok
        await http.aclose()
        return results

    results = run(go())
    assert results == {"good": True, "bad": False}
    good, bad = registry.sources["good"].tracker.status, registry.sources["bad"].tracker.status
    assert good.state == HealthState.HEALTHY.value and good.active
    assert bad.state == HealthState.DISCONNECTED.value and not bad.active and "ConnectError" in bad.last_error


def test_probe_fails_closed_on_empty_or_invalid_feed():
    registry = SourceRegistry([spec(id="empty"), spec(id="html", url="https://html.invalid/x")])
    http = client({"https://good.invalid/feed.xml": httpx.Response(200, content=rss([])),
                   "https://html.invalid/x": httpx.Response(200, content=b"<html>challenge</html>")})

    async def go():
        out = [await registry.probe(item, http, NOW) for item in registry.sources.values()]
        await http.aclose()
        return out

    assert run(go()) == [False, False]


def test_future_timestamp_is_rejected_and_nse_link_metadata_extracted():
    registry = SourceRegistry([spec()])
    http = client({"https://good.invalid/feed.xml": httpx.Response(200, content=GOOD)})
    item = registry.sources["s1"]

    async def go():
        await registry.probe(item, http, NOW)
        result = await registry.poll(item, http, since=None, now=NOW, interval_seconds=60)
        await http.aclose()
        return result

    result = run(go())
    by_title = {obs.title: obs for obs in result.observations}
    future = by_title["Future item"]
    assert future.published_at is None and "future_source_timestamp_rejected" in future.raw["timestamp_flags"]
    filing = by_title["Larsen & Toubro Limited"]
    assert filing.raw["nse_filing"]["symbol"] == "LT"
    assert filing.source_quality == "PRIMARY"


def test_robots_disallow_marks_source_unavailable():
    registry = SourceRegistry([spec()])
    http = client({"https://good.invalid/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /"),
                   "https://good.invalid/feed.xml": httpx.Response(200, content=GOOD)})

    async def go():
        ok = await registry.probe(registry.sources["s1"], http, NOW)
        await http.aclose()
        return ok

    assert run(go()) is False
    assert registry.sources["s1"].tracker.status.state == HealthState.UNAVAILABLE.value


def test_catalogue_resolution_uses_only_the_official_page_link():
    catalogue = b'<html><a href="/content/RSS/Online_announcements.xml">Announcements</a></html>'
    registry = SourceRegistry([spec(url=None, catalogue_url="https://cat.invalid/rss-feed",
                                    catalogue_match="Online_announcements\\.xml")])
    http = client({"https://cat.invalid/rss-feed": httpx.Response(200, content=catalogue),
                   "https://cat.invalid/content/RSS/Online_announcements.xml": httpx.Response(200, content=GOOD)})

    async def go():
        ok = await registry.probe(registry.sources["s1"], http, NOW)
        await http.aclose()
        return ok

    assert run(go()) is True
    assert registry.sources["s1"].tracker.status.resolved_url == "https://cat.invalid/content/RSS/Online_announcements.xml"


def test_health_degrades_to_error_then_disconnected_with_backoff():
    registry = SourceRegistry([spec()])
    item = registry.sources["s1"]
    item.tracker.record_probe(True, NOW)
    item.tracker.record_failure(NOW, "HTTP 503", interval_seconds=60)
    assert item.tracker.status.state == HealthState.ERROR.value
    first_backoff = item.tracker.status.next_due_at - NOW
    item.tracker.record_failure(NOW, "HTTP 503", interval_seconds=60)
    item.tracker.record_failure(NOW, "HTTP 503", interval_seconds=60)
    assert item.tracker.status.state == HealthState.DISCONNECTED.value
    assert item.tracker.status.next_due_at - NOW > first_backoff
    item.tracker.record_success(NOW, observations=3, latency_ms=10, newest_item_at=NOW, interval_seconds=60)
    assert item.tracker.status.state in (HealthState.HEALTHY.value, HealthState.DEGRADED.value)
    item.tracker.status.newest_item_at = NOW - timedelta(hours=10)
    registry2 = SourceRegistry([spec(stale_after_hours=6)])
    tracker = registry2.sources["s1"].tracker
    tracker.record_success(NOW, observations=1, latency_ms=5, newest_item_at=NOW - timedelta(hours=10), interval_seconds=60)
    assert tracker.status.state == HealthState.STALE.value


def test_unavailable_and_disabled_sources_are_reported_not_fetched():
    registry = SourceRegistry([spec(id="u", kind="unavailable", url=None, notes="no documented feed"),
                               spec(id="d", activation=Activation.DISABLED)])
    for item in registry.sources.values():
        assert item.tracker.status.state == HealthState.UNAVAILABLE.value
        assert registry.needs_probe(item, NOW) is False


def test_google_news_publisher_split_and_discovery_quality():
    feed = rss([("Larsen & Toubro bags order - The Hindu BusinessLine", "https://news.google.com/rss/articles/abc",
                 "Mon, 05 Oct 2026 03:00:00 GMT")])
    registry = SourceRegistry([spec(id="gn", kind="google_news", quality=SourceQuality.DISCOVERY_ONLY,
                                    url="https://news.google.com/rss/search", queries=("L&T order",))])
    http = client({"https://news.google.com/rss/search": httpx.Response(200, content=feed)})

    async def go():
        await registry.probe(registry.sources["gn"], http, NOW)
        result = await registry.poll(registry.sources["gn"], http, since=None, now=NOW, interval_seconds=60)
        await http.aclose()
        return result

    item = run(go()).observations[0]
    assert item.title == "Larsen & Toubro bags order"
    assert item.publisher == "The Hindu BusinessLine"
    assert item.source_quality == "DISCOVERY_ONLY" and item.raw["discovery_query"] == "L&T order"


def test_gdelt_and_nse_deal_csv_parsing():
    gdelt = b'{"articles":[{"url":"https://x.invalid/a","title":"Tata Steel shares jump","seendate":"20261005T033000Z","domain":"x.invalid","language":"English","sourcecountry":"India"}]}'
    csv = b"Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n05-OCT-2026,LT,Larsen,ABC Fund,BUY,100000,3500.5,-\n"
    registry = SourceRegistry([
        spec(id="g", kind="gdelt", quality=SourceQuality.DISCOVERY_ONLY, url="https://api.gdelt.invalid/doc", queries=("q",)),
        spec(id="b", kind="nse_deals_csv", url="https://arch.invalid/block.csv", params={"deal_kind": "block"}),
    ])
    http = client({"https://api.gdelt.invalid/doc": httpx.Response(200, content=gdelt),
                   "https://arch.invalid/block.csv": httpx.Response(200, content=csv)})

    async def go():
        out = {}
        for key, item in registry.sources.items():
            await registry.probe(item, http, NOW)
            out[key] = await registry.poll(item, http, since=None, now=NOW, interval_seconds=60)
        await http.aclose()
        return out

    out = run(go())
    article = out["g"].observations[0]
    assert article.published_at == datetime(2026, 10, 5, 3, 30, tzinfo=timezone.utc)
    deal = out["b"].observations[0]
    assert deal.raw["nse_deal"]["symbol"] == "LT" and "block deal" in deal.title


def test_persisted_provider_history_survives_restart():
    registry = SourceRegistry([spec()], persisted={"s1": {"provider_id": "s1", "total_attempts": 40, "total_failures": 2,
                                                          "total_observations": 900, "last_success_at": NOW.isoformat()}})
    status = registry.sources["s1"].tracker.status
    assert status.total_attempts == 40 and status.total_observations == 900
    assert status.active is False  # activation is re-earned by a fresh probe


@pytest.mark.parametrize("path", [Path("config/sources.yaml")])
def test_sources_file_has_no_duplicate_ids(path):
    specs = load_source_specs(Path(__file__).resolve().parents[1] / path)
    assert len({item.id for item in specs}) == len(specs)


def test_robots_txt_follows_rfc_9309():
    async def check(robots_response):
        http = client({"https://good.invalid/robots.txt": robots_response,
                       "https://good.invalid/feed.xml": httpx.Response(200, content=GOOD)})
        try:
            return await http.allowed("https://good.invalid/feed.xml")
        except Exception as exc:  # noqa: BLE001
            return type(exc).__name__
        finally:
            await http.aclose()

    assert run(check(httpx.Response(403))) is True       # 4xx: no restrictions
    assert run(check(httpx.Response(404))) is True
    assert run(check(httpx.Response(503))) == "SourceError"  # 5xx: transient complete disallow
    assert run(check(httpx.ConnectError("x"))) == "SourceError"
    assert run(check(httpx.Response(200, text="User-agent: test-agent\nDisallow: /feed.xml"))) is False
