from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx


@dataclass(frozen=True)
class RawObservation:
    provider_id: str
    source_tier: int
    publisher: str
    title: str
    url: str
    summary: str
    published_at: datetime | None
    observed_at: datetime
    raw: dict[str, Any]


class RSSAcquirer:
    """Fetch RSS without interpreting its content.

    Acquisition is deliberately dumb: it records what the source published and
    leaves normalization, entity resolution, clustering and interpretation to
    later layers.
    """

    def __init__(self, timeout_seconds: float = 15.0) -> None:
        self.timeout = httpx.Timeout(timeout_seconds)

    def fetch(
        self,
        *,
        provider_id: str,
        publisher: str,
        url: str,
        source_tier: int,
        since: datetime | None = None,
    ) -> list[RawObservation]:
        response = httpx.get(
            url,
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "PSYGRIDEVENTS/0.1 (+event-intelligence)"},
        )
        response.raise_for_status()
        parsed = feedparser.parse(response.content)
        now = datetime.now(timezone.utc)
        observations: list[RawObservation] = []

        for entry in parsed.entries:
            published = self._entry_datetime(entry)
            if since and published and published <= since:
                continue
            title = str(entry.get("title", "")).strip()
            link = str(entry.get("link", "")).strip()
            if not title or not link:
                continue
            summary = str(entry.get("summary", entry.get("description", ""))).strip()
            observations.append(
                RawObservation(
                    provider_id=provider_id,
                    source_tier=source_tier,
                    publisher=publisher,
                    title=title,
                    url=link,
                    summary=summary,
                    published_at=published,
                    observed_at=now,
                    raw=dict(entry),
                )
            )
        return observations

    @staticmethod
    def _entry_datetime(entry: Any) -> datetime | None:
        parsed = entry.get("published_parsed") or entry.get("updated_parsed")
        if not parsed:
            return None
        return datetime(*parsed[:6], tzinfo=timezone.utc)
