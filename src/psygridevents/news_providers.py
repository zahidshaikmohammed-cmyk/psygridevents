from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Protocol

from .acquisition import RawObservation


@dataclass(frozen=True, slots=True)
class ProviderMessage:
    """Provider-neutral news message before PSYGRID event normalization."""

    provider_id: str
    publisher: str
    title: str
    url: str = ""
    summary: str = ""
    published_at: datetime | None = None
    raw: dict[str, Any] | None = None


class LicensedNewsProvider(Protocol):
    """Contract for licensed/provider-specific news adapters.

    Adapters must remain responsible only for transport + schema mapping.
    Event interpretation belongs to the existing PSYGRIDEVENTS pipeline.
    """

    provider_id: str

    def normalize_messages(
        self, messages: Iterable[ProviderMessage]
    ) -> list[RawObservation]:
        ...


class MessageNormalizer:
    """Convert provider messages into the existing RawObservation contract."""

    def normalize_messages(
        self, messages: Iterable[ProviderMessage], *, source_tier: int = 1
    ) -> list[RawObservation]:
        now = datetime.now(timezone.utc)
        observations: list[RawObservation] = []

        for message in messages:
            title = message.title.strip()
            if not title:
                continue

            observations.append(
                RawObservation(
                    provider_id=message.provider_id,
                    source_tier=source_tier,
                    publisher=message.publisher.strip() or message.provider_id,
                    title=title,
                    url=message.url.strip(),
                    summary=message.summary.strip(),
                    published_at=message.published_at,
                    observed_at=now,
                    raw=dict(message.raw or {}),
                )
            )

        return observations
