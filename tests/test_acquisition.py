from datetime import datetime, timedelta, timezone

import httpx
import pytest

from psygridevents.acquisition import RSSAcquirer

RSS_XML = """<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Test Feed</title>
<item>
  <title>Company wins major order</title>
  <link>https://example.com/a</link>
  <description>Summary text</description>
  <pubDate>Mon, 21 Sep 2026 10:00:00 GMT</pubDate>
</item>
<item>
  <title></title>
  <link></link>
  <pubDate>Mon, 21 Sep 2026 10:05:00 GMT</pubDate>
</item>
</channel></rss>
"""


def _acquirer(handler) -> RSSAcquirer:
    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    return RSSAcquirer(client=client)


def test_fetch_parses_a_well_formed_feed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=RSS_XML.encode("utf-8"))

    observations = _acquirer(handler).fetch(provider_id="p", publisher="Pub", url="https://feed.example/rss", source_tier=0)
    assert len(observations) == 1
    assert observations[0].title == "Company wins major order"
    assert observations[0].url == "https://example.com/a"


def test_entries_missing_title_or_link_are_dropped_not_fabricated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=RSS_XML.encode("utf-8"))

    observations = _acquirer(handler).fetch(provider_id="p", publisher="Pub", url="https://feed.example/rss", source_tier=0)
    # The second <item> has empty title/link and must not appear.
    assert all(o.title and o.url for o in observations)


def test_malformed_xml_degrades_to_zero_observations_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"this is not xml at all <<<>>>")

    observations = _acquirer(handler).fetch(provider_id="p", publisher="Pub", url="https://feed.example/rss", source_tier=0)
    assert observations == []


def test_empty_feed_yields_no_observations() -> None:
    empty = b'<?xml version="1.0"?><rss version="2.0"><channel><title>Empty</title></channel></rss>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=empty)

    observations = _acquirer(handler).fetch(provider_id="p", publisher="Pub", url="https://feed.example/rss", source_tier=0)
    assert observations == []


def test_http_error_status_raises_and_is_not_silently_swallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(httpx.HTTPStatusError):
        _acquirer(handler).fetch(provider_id="p", publisher="Pub", url="https://feed.example/rss", source_tier=0)


def test_timeout_raises_and_is_not_silently_swallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out", request=request)

    with pytest.raises(httpx.TimeoutException):
        _acquirer(handler).fetch(provider_id="p", publisher="Pub", url="https://feed.example/rss", source_tier=0)


def test_connection_failure_raises_and_is_not_silently_swallowed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(httpx.ConnectError):
        _acquirer(handler).fetch(provider_id="p", publisher="Pub", url="https://feed.example/rss", source_tier=0)


def test_since_filters_out_already_seen_entries() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=RSS_XML.encode("utf-8"))

    since = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)  # exactly the entry's pubDate
    observations = _acquirer(handler).fetch(
        provider_id="p", publisher="Pub", url="https://feed.example/rss", source_tier=0, since=since,
    )
    assert observations == []

    earlier = since - timedelta(minutes=1)
    observations = _acquirer(handler).fetch(
        provider_id="p", publisher="Pub", url="https://feed.example/rss", source_tier=0, since=earlier,
    )
    assert len(observations) == 1


def test_raw_provider_payload_is_preserved_on_the_observation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=RSS_XML.encode("utf-8"))

    observations = _acquirer(handler).fetch(provider_id="p", publisher="Pub", url="https://feed.example/rss", source_tier=0)
    assert "title" in observations[0].raw
