"""Concrete public-source adapters. Each one only records what the source published."""
from __future__ import annotations

import csv
import html
import io
import json
import re
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote_plus, urljoin, urlsplit

import feedparser

from ..acquisition import RawObservation
from ..nse_parsing import parse_nse_filing_link
from .base import FetchResult, SourceAdapter, SourceError, SourceSpec
from .http import SourceHttpClient

_WS = re.compile(r"\s+")
_TAGS = re.compile(r"<[^>]+>")


def _text(value: Any) -> str:
    if value is None:
        return ""
    return _WS.sub(" ", html.unescape(_TAGS.sub(" ", str(value)))).strip()


def _entry_datetime(entry: Any) -> datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _observation(
    spec: SourceSpec,
    *,
    title: str,
    url: str,
    summary: str,
    published_at: datetime | None,
    observed_at: datetime,
    publisher: str | None = None,
    raw: dict[str, Any] | None = None,
) -> RawObservation:
    return RawObservation(
        provider_id=spec.id,
        source_tier=spec.tier,
        publisher=publisher or spec.name,
        title=title,
        url=url,
        summary=summary,
        published_at=published_at,
        observed_at=observed_at,
        raw=raw or {},
        source_quality=spec.quality.value,
        category=spec.category,
    )


def _sanitize_published(published: datetime | None, now: datetime) -> tuple[datetime | None, list[str]]:
    """Reject impossible timestamps instead of trusting them.

    A publication time more than 5 minutes in the future is a source clock or
    timezone error; it is dropped (the observation falls back to first-seen
    time downstream) and the fact is recorded on the observation.
    """
    flags: list[str] = []
    if published is None:
        return None, ["no_source_timestamp"]
    if published > now + timedelta(minutes=5):
        flags.append("future_source_timestamp_rejected")
        return None, flags
    return published, flags


class RSSSourceAdapter(SourceAdapter):
    """RSS/Atom feed. Optionally resolves its concrete URL from an official catalogue page."""

    async def resolve_from_catalogue(self, http: SourceHttpClient) -> str | None:
        if not self.spec.catalogue_url or not self.spec.catalogue_match:
            return None
        response = await http.get(self.spec.catalogue_url, conditional=False)
        pattern = re.compile(self.spec.catalogue_match)
        for href in _extract_links(response.content.decode("utf-8", "replace"), base=response.url):
            if pattern.search(href[0]):
                return href[0]
        return None

    async def probe(self, http: SourceHttpClient, *, now: datetime) -> FetchResult:
        notes: list[str] = []
        if self.spec.catalogue_url:
            try:
                resolved = await self.resolve_from_catalogue(http)
            except SourceError as exc:
                resolved = None
                notes.append(f"catalogue unavailable: {exc}")
            if resolved:
                self.resolved_url = resolved
                notes.append(f"feed url resolved from official catalogue {self.spec.catalogue_url}")
        if not self.resolved_url:
            raise SourceError("no feed URL: catalogue did not list a matching feed and no documented URL is configured")
        result = await self.fetch(http, since=None, now=now, conditional=False)
        if result.items_seen == 0:
            raise SourceError("feed parsed but contains no items; endpoint not validated")
        result.notes = tuple(notes) + result.notes
        return result

    async def fetch(
        self, http: SourceHttpClient, *, since: datetime | None, now: datetime, conditional: bool = True
    ) -> FetchResult:
        url = self.resolved_url
        if not url:
            raise SourceError("source has no resolved URL")
        response = await http.get(url, conditional=conditional)
        if response.not_modified:
            return FetchResult([], url, 304, True, 0, response.latency_ms)
        parsed = feedparser.parse(response.content)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            raise SourceError("response is not a valid RSS/Atom document")
        observations: list[RawObservation] = []
        newest: datetime | None = None
        for entry in parsed.entries[: self.spec.max_items]:
            title = _text(entry.get("title"))
            link = _text(entry.get("link"))
            if not title or not link:
                continue
            published, flags = _sanitize_published(_entry_datetime(entry), now)
            if published and (newest is None or published > newest):
                newest = published
            if since and published and published <= since:
                continue
            summary = _text(entry.get("summary") or entry.get("description"))
            publisher, title = self._publisher_and_title(entry, title)
            raw: dict[str, Any] = {"guid": _text(entry.get("id")), "timestamp_flags": flags}
            filing = parse_nse_filing_link(link)
            if filing is not None:
                raw["nse_filing"] = {
                    "symbol": filing.symbol,
                    "disseminated_at": filing.disseminated_at.isoformat(),
                    "subject": filing.subject,
                }
            observations.append(
                _observation(
                    self.spec, title=title, url=link, summary=summary[:2000], published_at=published,
                    observed_at=now, publisher=publisher, raw=raw,
                )
            )
        return FetchResult(
            observations, url, 200, False, len(response.content), response.latency_ms,
            items_seen=len(parsed.entries), newest_item_at=newest,
        )

    def _publisher_and_title(self, entry: Any, title: str) -> tuple[str, str]:
        return self.spec.name, title


class GoogleNewsSourceAdapter(RSSSourceAdapter):
    """Google News RSS search results. DISCOVERY_ONLY: headlines + links, never confirmation."""

    def __init__(self, spec: SourceSpec) -> None:
        super().__init__(spec)
        self.base = spec.url or "https://news.google.com/rss/search"
        self.edition = spec.params.get("edition", {"hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
        self.window = str(spec.params.get("when", "1d"))
        self.resolved_url = self._query_url(spec.queries[0]) if spec.queries else None

    def _query_url(self, query: str) -> str:
        q = quote_plus(f"{query} when:{self.window}")
        return f"{self.base}?q={q}&hl={self.edition['hl']}&gl={self.edition['gl']}&ceid={quote_plus(self.edition['ceid'])}"

    async def probe(self, http: SourceHttpClient, *, now: datetime) -> FetchResult:
        if not self.spec.queries:
            raise SourceError("no queries configured")
        self.resolved_url = self._query_url(self.spec.queries[0])
        result = await RSSSourceAdapter.fetch(self, http, since=None, now=now, conditional=False)
        return result

    async def fetch(
        self, http: SourceHttpClient, *, since: datetime | None, now: datetime, conditional: bool = True
    ) -> FetchResult:
        observations: list[RawObservation] = []
        items = 0
        size = 0
        newest: datetime | None = None
        errors: list[str] = []
        for query in self.spec.queries:
            self.resolved_url = self._query_url(query)
            try:
                result = await RSSSourceAdapter.fetch(self, http, since=since, now=now, conditional=conditional)
            except SourceError as exc:
                errors.append(f"{query}: {exc}")
                continue
            for item in result.observations:
                item.raw["discovery_query"] = query
            observations.extend(result.observations)
            items += result.items_seen
            size += result.bytes_received
            if result.newest_item_at and (newest is None or result.newest_item_at > newest):
                newest = result.newest_item_at
        if errors and not observations and len(errors) == len(self.spec.queries):
            raise SourceError("; ".join(errors)[:500])
        return FetchResult(observations, self.base, 200, False, size, None, items, newest, tuple(errors))

    def _publisher_and_title(self, entry: Any, title: str) -> tuple[str, str]:
        source = entry.get("source") or {}
        publisher = _text(source.get("title") if isinstance(source, dict) else getattr(source, "title", "")) or "Google News"
        suffix = f" - {publisher}"
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
        return publisher, title


class GdeltSourceAdapter(SourceAdapter):
    """GDELT DOC 2.0 API (documented, free). DISCOVERY_ONLY; ~15 minute update cadence."""

    API = "https://api.gdeltproject.org/api/v2/doc/doc"

    async def fetch(self, http: SourceHttpClient, *, since: datetime | None, now: datetime) -> FetchResult:
        observations: list[RawObservation] = []
        items = 0
        size = 0
        newest: datetime | None = None
        errors: list[str] = []
        timespan = str(self.spec.params.get("timespan", "60min"))
        max_records = int(self.spec.params.get("maxrecords", 75))
        for query in self.spec.queries:
            params = {
                "query": query, "mode": "artlist", "format": "json", "maxrecords": str(max_records),
                "timespan": timespan, "sort": "datedesc",
            }
            try:
                response = await http.get(self.spec.url or self.API, params=params, conditional=False)
                payload = json.loads(response.content.decode("utf-8", "replace") or "{}")
            except (SourceError, json.JSONDecodeError) as exc:
                errors.append(f"{query}: {exc}")
                continue
            size += len(response.content)
            for article in payload.get("articles", []) or []:
                items += 1
                title = _text(article.get("title"))
                url = _text(article.get("url"))
                if not title or not url:
                    continue
                published = None
                seen = str(article.get("seendate") or "")
                try:
                    published = datetime.strptime(seen, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    published = None
                published, flags = _sanitize_published(published, now)
                if published and (newest is None or published > newest):
                    newest = published
                if since and published and published <= since:
                    continue
                observations.append(
                    _observation(
                        self.spec, title=title, url=url, summary="", published_at=published, observed_at=now,
                        publisher=_text(article.get("domain")) or "GDELT",
                        raw={
                            "discovery_query": query, "language": article.get("language"),
                            "sourcecountry": article.get("sourcecountry"), "timestamp_flags": flags,
                            "timestamp_basis": "gdelt_seendate",
                        },
                    )
                )
        if errors and len(errors) == len(self.spec.queries):
            raise SourceError("; ".join(errors)[:500])
        return FetchResult(observations, self.spec.url or self.API, 200, False, size, None, items, newest, tuple(errors))


class NSEDealsCsvAdapter(SourceAdapter):
    """NSE bulk/block deal CSV published in the NSE archives (exchange first-party data)."""

    REQUIRED = ("symbol",)

    async def fetch(self, http: SourceHttpClient, *, since: datetime | None, now: datetime) -> FetchResult:
        if not self.resolved_url:
            raise SourceError("no documented URL configured")
        response = await http.get(self.resolved_url)
        if response.not_modified:
            return FetchResult([], self.resolved_url, 304, True, 0, response.latency_ms)
        text = response.content.decode("utf-8-sig", "replace")
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or not any("symbol" in (name or "").lower() for name in reader.fieldnames):
            raise SourceError("CSV does not have the documented Symbol column")
        observations: list[RawObservation] = []
        items = 0
        deal_kind = str(self.spec.params.get("deal_kind", "deal"))
        for row in reader:
            items += 1
            normalized = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k}
            symbol = next((v for k, v in normalized.items() if k == "symbol"), "")
            if not symbol:
                continue
            client = next((v for k, v in normalized.items() if "client" in k), "")
            side = next((v for k, v in normalized.items() if "buy" in k and "sell" in k), "")
            quantity = next((v for k, v in normalized.items() if "quantity" in k), "")
            price = next((v for k, v in normalized.items() if "price" in k), "")
            deal_date = next((v for k, v in normalized.items() if k == "date"), "")
            title = f"{symbol}: {deal_kind} deal - {client} {side.lower()} {quantity} shares at {price}".strip()
            url = f"{self.resolved_url}#{symbol}-{client}-{side}-{quantity}-{price}-{deal_date}".replace(" ", "_")
            observations.append(
                _observation(
                    self.spec, title=title, url=url, summary=f"Exchange-reported {deal_kind} deal dated {deal_date}.",
                    published_at=None, observed_at=now,
                    raw={"nse_deal": {**normalized, "symbol": symbol.upper()}, "timestamp_basis": "first_seen_date_only"},
                )
            )
        return FetchResult(observations, self.resolved_url, 200, False, len(response.content), response.latency_ms, items)

    async def probe(self, http: SourceHttpClient, *, now: datetime) -> FetchResult:
        return await self.fetch(http, since=None, now=now)


class _LinkCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, _WS.sub(" ", "".join(self._text)).strip()))
            self._href = None


def _extract_links(document: str, *, base: str) -> list[tuple[str, str]]:
    collector = _LinkCollector()
    try:
        collector.feed(document)
    except Exception:  # noqa: BLE001 - malformed HTML yields whatever links were parsed
        pass
    result = []
    for href, text in collector.links:
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        result.append((urljoin(base, href), text))
    return result


class HtmlLinksSourceAdapter(SourceAdapter):
    """Official listing page watcher: each new link on the page becomes one observation.

    Only for public listing pages (no login, no CAPTCHA, robots.txt allowed).
    The page's links carry no reliable timestamp, so observations are clocked
    at first-seen time and flagged as such.
    """

    async def fetch(self, http: SourceHttpClient, *, since: datetime | None, now: datetime) -> FetchResult:
        if not self.resolved_url:
            raise SourceError("no documented URL configured")
        response = await http.get(self.resolved_url)
        if response.not_modified:
            return FetchResult([], self.resolved_url, 304, True, 0, response.latency_ms)
        document = response.content.decode("utf-8", "replace")
        include = re.compile(self.spec.include_pattern or r".", re.IGNORECASE)
        own_host = urlsplit(self.resolved_url).netloc.split(":")[0].lower()
        observations: list[RawObservation] = []
        seen: set[str] = set()
        items = 0
        for url, text in _extract_links(document, base=response.url):
            host = urlsplit(url).netloc.split(":")[0].lower()
            if not host.endswith(own_host.split("www.")[-1]):
                continue
            if not text or len(text) < 12 or not include.search(f"{text} {url}"):
                continue
            if url in seen:
                continue
            seen.add(url)
            items += 1
            observations.append(
                _observation(
                    self.spec, title=text[:400], url=url, summary="", published_at=None, observed_at=now,
                    raw={"timestamp_basis": "first_seen_on_listing_page"},
                )
            )
            if items >= self.spec.max_items:
                break
        return FetchResult(observations, self.resolved_url, 200, False, len(response.content), response.latency_ms, items)

    async def probe(self, http: SourceHttpClient, *, now: datetime) -> FetchResult:
        result = await self.fetch(http, since=None, now=now)
        if result.items_seen == 0:
            raise SourceError("listing page returned no matching links; endpoint not validated")
        return result


class UnavailableSourceAdapter(SourceAdapter):
    """A source with no documented public machine-readable endpoint.

    It exists so the source matrix is complete and honest: the provider is
    listed, reported as UNAVAILABLE with the reason, and never fabricated.
    """

    async def fetch(self, http: SourceHttpClient, *, since: datetime | None, now: datetime) -> FetchResult:
        raise SourceError(self.spec.notes or "no documented public endpoint")


ADAPTERS: dict[str, type[SourceAdapter]] = {
    "rss": RSSSourceAdapter,
    "google_news": GoogleNewsSourceAdapter,
    "gdelt": GdeltSourceAdapter,
    "nse_deals_csv": NSEDealsCsvAdapter,
    "html_links": HtmlLinksSourceAdapter,
    "unavailable": UnavailableSourceAdapter,
}


def build_adapter(spec: SourceSpec) -> SourceAdapter:
    try:
        cls = ADAPTERS[spec.kind]
    except KeyError as exc:
        raise ValueError(f"unknown source kind {spec.kind!r} for {spec.id}") from exc
    return cls(spec)


__all__ = [
    "ADAPTERS", "GdeltSourceAdapter", "GoogleNewsSourceAdapter", "HtmlLinksSourceAdapter", "NSEDealsCsvAdapter",
    "RSSSourceAdapter", "UnavailableSourceAdapter", "build_adapter",
]
