from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

import feedparser
import httpx


class CatalogueResolutionError(RuntimeError):
    """Raised when an official catalogue cannot be resolved safely."""


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def extract_feed_links(catalogue_url: str, html: str, *, allowed_hosts: set[str] | None = None) -> tuple[str, ...]:
    """Extract only concrete feed-looking links present in an official catalogue document."""
    allowed_hosts = allowed_hosts or {urlparse(catalogue_url).netloc}
    hrefs = re.findall(r"href=[\"']([^\"']+)[\"']", html, re.IGNORECASE)
    links: list[str] = []
    for href in hrefs:
        candidate = urljoin(catalogue_url, href).strip()
        if not _is_http_url(candidate) or urlparse(candidate).netloc not in allowed_hosts:
            continue
        lower = candidate.lower()
        if not any(token in lower for token in ("rss", "feed", "xml")):
            continue
        if candidate not in links:
            links.append(candidate)
    return tuple(links)


def validate_rss_url(url: str, *, client: httpx.Client | None = None) -> bool:
    """Return true only when a discovered URL actually parses as RSS/Atom with entries."""
    if not _is_http_url(url):
        return False
    owns_client = client is None
    client = client or httpx.Client(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "PSYGRIDEVENTS/0.2 (+official-feed-discovery)"},
    )
    try:
        response = client.get(url)
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        return not (getattr(parsed, "bozo", False) and not parsed.entries) and bool(parsed.entries)
    except (httpx.HTTPError, ValueError, TypeError):
        return False
    finally:
        if owns_client:
            client.close()


def resolve_official_rss_catalogue(catalogue_url: str, *, client: httpx.Client | None = None) -> tuple[str, ...]:
    """Resolve and validate only feed links explicitly published by an official catalogue."""
    owns_client = client is None
    client = client or httpx.Client(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "PSYGRIDEVENTS/0.2 (+official-feed-discovery)"},
    )
    try:
        response = client.get(catalogue_url)
        response.raise_for_status()
        links = extract_feed_links(catalogue_url, response.text)
        return tuple(link for link in links if validate_rss_url(link, client=client))
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise CatalogueResolutionError(f"Official RSS catalogue resolution failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()
