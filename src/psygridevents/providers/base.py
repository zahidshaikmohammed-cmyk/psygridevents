"""Provider-neutral source contracts: specs, quality classes, fetch results."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ..acquisition import RawObservation

if TYPE_CHECKING:
    from .http import SourceHttpClient


class SourceQuality(StrEnum):
    """Evidence class of a source. This is an authority class, not a truth guarantee.

    PRIMARY             -- the issuer/exchange disclosure itself (NSE/BSE filings, exchange data).
    OFFICIAL            -- a regulator / central bank / government body publishing its own action.
    REPUTABLE_SECONDARY -- established financial media reporting on events.
    DISCOVERY_ONLY      -- aggregators/search (GDELT, Google News): leads, never confirmation by themselves.
    """

    PRIMARY = "PRIMARY"
    OFFICIAL = "OFFICIAL"
    REPUTABLE_SECONDARY = "REPUTABLE_SECONDARY"
    DISCOVERY_ONLY = "DISCOVERY_ONLY"


# Legacy numeric evidence tier used by the CP0-CP11 engines (0 = first party).
QUALITY_TIER: dict[str, int] = {
    SourceQuality.PRIMARY: 0,
    SourceQuality.OFFICIAL: 0,
    SourceQuality.REPUTABLE_SECONDARY: 2,
    SourceQuality.DISCOVERY_ONLY: 4,
}

QUALITY_CONFIDENCE: dict[str, float] = {
    SourceQuality.PRIMARY: 1.0,
    SourceQuality.OFFICIAL: 0.95,
    SourceQuality.REPUTABLE_SECONDARY: 0.7,
    SourceQuality.DISCOVERY_ONLY: 0.4,
}

QUALITY_RANK: dict[str, int] = {
    SourceQuality.PRIMARY: 0,
    SourceQuality.OFFICIAL: 1,
    SourceQuality.REPUTABLE_SECONDARY: 2,
    SourceQuality.DISCOVERY_ONLY: 3,
}


def best_quality(values: list[str] | tuple[str, ...]) -> str:
    candidates = [value for value in values if value in QUALITY_RANK]
    if not candidates:
        return SourceQuality.DISCOVERY_ONLY.value
    return min(candidates, key=lambda value: QUALITY_RANK[value])


class Activation(StrEnum):
    AUTO = "auto"  # probe, then poll if the probe validates the endpoint
    DISABLED = "disabled"
    UNAVAILABLE = "unavailable"  # no documented public endpoint; adapter interface only


@dataclass(frozen=True)
class SourceSpec:
    id: str
    name: str
    kind: str  # rss | google_news | gdelt | nse_deals_csv | html_links | unavailable
    quality: SourceQuality
    category: str
    url: str | None = None
    queries: tuple[str, ...] = ()
    activation: Activation = Activation.AUTO
    verification: str = "probe_required"
    evidence: str = ""
    poll_seconds: float = 120.0
    off_hours_poll_seconds: float | None = None
    stale_after_hours: float | None = None
    catalogue_url: str | None = None
    catalogue_match: str | None = None
    include_pattern: str | None = None
    max_items: int = 200
    params: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    @property
    def tier(self) -> int:
        return QUALITY_TIER[self.quality]


@dataclass
class FetchResult:
    observations: list[RawObservation]
    url: str | None
    http_status: int | None = None
    not_modified: bool = False
    bytes_received: int = 0
    latency_ms: float | None = None
    items_seen: int = 0
    newest_item_at: datetime | None = None
    notes: tuple[str, ...] = ()


class SourceError(RuntimeError):
    """A provider failed to fetch/parse. Carries a short, loggable reason."""


class RobotsDisallowed(SourceError):
    """The site's robots.txt disallows our user agent for this URL."""


class SourceAdapter(ABC):
    def __init__(self, spec: SourceSpec) -> None:
        self.spec = spec
        self.resolved_url: str | None = spec.url

    @abstractmethod
    async def fetch(self, http: "SourceHttpClient", *, since: datetime | None, now: datetime) -> FetchResult:
        raise NotImplementedError

    async def probe(self, http: "SourceHttpClient", *, now: datetime) -> FetchResult:
        """Validate that the endpoint exists and returns the documented structure.

        The default probe is simply a fetch with no watermark: a source that
        cannot be fetched and parsed into at least the documented structure
        is never activated.
        """
        return await self.fetch(http, since=None, now=now)
