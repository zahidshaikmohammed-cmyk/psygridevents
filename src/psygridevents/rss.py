from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx


@dataclass(frozen=True, slots=True)
class RSSFeedSpec:
    provider_id: str
    name: str
    url: str
    tier: int
    category: str


class RSSFetchError(RuntimeError):
    """Raised when an RSS source cannot be fetched or parsed safely."""


def _parse_datetime(entry: Any) -> datetime | None:
    """Convert feedparser time tuples to timezone-aware UTC datetimes."""
    value = entry.get("published_parsed") or entry.get("updated_parsed")
    if not value:
        return None
    try:
        return datetime(*value[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


class RSSAdapter:
    """Fetch and normalize RSS records without adding interpretation.

    The adapter's only job is transport + normalization. Event classification,
    entity mapping, materiality and market interpretation belong downstream.
    """

    def __init__(self, spec: RSSFeedSpec, *, timeout_seconds: float = 15.0) -> None:
        self.spec = spec
        self.timeout_seconds = timeout_seconds

    def fetch(self, since: datetime | None = None) -> list[dict[str, Any]]:
        try:
            response = httpx.get(
                self.spec.url,
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers={
                    "User-Agent": "PSYGRIDEVENTS/0.1 (+precision-first-event-intelligence)"
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise RSSFetchError(f"{self.spec.provider_id}: HTTP fetch failed: {exc}") from exc

        parsed = feedparser.parse(response.content)
        if getattr(parsed, "bozo", False) and not parsed.entries:
            raise RSSFetchError(f"{self.spec.provider_id}: invalid RSS/Atom document")

        records: list[dict[str, Any]] = []
        for entry in parsed.entries:
            published_at = _parse_datetime(entry)
            if since and published_at and published_at <= since.astimezone(timezone.utc):
                continue

            title = _text(entry.get("title"))
            link = _text(entry.get("link"))
            if not title or not link:
                # Do not manufacture incomplete evidence records.
                continue

            records.append(
                {
                    "provider_id": self.spec.provider_id,
                    "publisher": self.spec.name,
                    "source_tier": self.spec.tier,
                    "category": self.spec.category,
                    "title": title,
                    "url": link,
                    "published_at": published_at,
                    "summary": _text(entry.get("summary") or entry.get("description")),
                    "guid": _text(entry.get("id")),
                    "raw": dict(entry),
                }
            )

        return records
