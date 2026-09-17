from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone

from .models import Event, EventStatus, EventType, SourceEvidence
from .scoring import ScoreInputs, calculate_priority


_WHITESPACE = re.compile(r"\s+")


def normalize_text(text: str) -> str:
    return _WHITESPACE.sub(" ", text.strip()).lower()


def event_fingerprint(title: str, publisher: str, event_date: datetime | None) -> str:
    """Create a stable fingerprint for coarse duplicate detection.

    This intentionally does not claim two articles are the same event. A later
    semantic clustering layer should make that decision using extracted facts.
    """
    day = event_date.date().isoformat() if event_date else "unknown"
    canonical = "|".join((normalize_text(title), normalize_text(publisher), day))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def build_event(
    *,
    title: str,
    summary: str,
    publisher: str,
    url: str,
    event_type: EventType = EventType.OTHER,
    status: EventStatus = EventStatus.UNCONFIRMED,
    event_time: datetime | None = None,
    instruments: list[str] | None = None,
    entities: list[str] | None = None,
    sectors: list[str] | None = None,
    source_tier: int = 3,
    published_at: datetime | None = None,
) -> Event:
    now = datetime.now(timezone.utc)
    return Event(
        event_id=event_fingerprint(title, publisher, event_time or published_at),
        observed_at=now,
        event_time=event_time,
        event_type=event_type,
        status=status,
        headline=title.strip(),
        factual_summary=summary.strip(),
        entities=entities or [],
        instruments=instruments or [],
        sectors=sectors or [],
        sources=[
            SourceEvidence(
                publisher=publisher,
                url=url,
                published_at=published_at,
                source_tier=source_tier,
                title=title.strip(),
                excerpt=summary.strip(),
            )
        ],
        first_seen_at=now,
    )


def assess_event(event: Event, inputs: ScoreInputs):
    return calculate_priority(event, inputs)
