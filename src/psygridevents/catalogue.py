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


def extract_feed_links(
    catalogue_url: str,
    html: str,
    *,
    allowed_hosts: set[str] | None = None,
) -> tuple[str, ...]:
    """Extract feed-looking URLs that are explicitly linked by an official catalogue."""
    catalogue_host = urlparse(catalogue_url).netloc
    hrefs = re.findall(r"href=[\"']([^\"']+)[\"']", html, re.IGNORECASE)
    links: list[str] = []
    for href in hrefs:
        candidate = urljoin(catalogue_url, href).strip()
        if not _is_http_url(candidate):
            continue
        host = urlparse(candidate).netloc
        if allowed_hosts is None:
            if host != catalogue_host:
                continue
        elif host not in allowed_hosts:
            continue
        lower = candidate.lower()
        if not any(token in lower for token in ("rss", "feed", "xml")):
            continue
        if candidate not in links:
            links.append(candidate)
    return tuple(links)


def validate_rss_url(url: str, *, client: httpx.Client | None = None) -> bool:
    """Return true only when a candidate actually parses as a non-empty RSS/Atom feed."""
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


def resolve_official_rss_catalogue(
    catalogue_url: str,
    *,
    client: httpx.Client | None = None,
    allowed_hosts: set[str] | None = None,
) -> tuple[str, ...]:
    """Resolve and validate only links published by an official RSS catalogue."""
    owns_client = client is None
    client = client or httpx.Client(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "PSYGRIDEVENTS/0.2 (+official-feed-discovery)"},
    )
    try:
        response = client.get(catalogue_url)
        response.raise_for_status()
        links = extract_feed_links(catalogue_url, response.text, allowed_hosts=allowed_hosts)
        return tuple(link for link in links if validate_rss_url(link, client=client))
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        raise CatalogueResolutionError(f"Official RSS catalogue resolution failed: {exc}") from exc
    finally:
        if owns_client:
            client.close()
