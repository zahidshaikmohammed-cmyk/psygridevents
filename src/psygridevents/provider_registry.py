from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .rss import RSSAdapter, RSSFeedSpec


@dataclass(frozen=True, slots=True)
class ProviderSpec:
    id: str
    name: str
    tier: int
    role: tuple[str, ...]
    transport: str
    enabled: bool
    official_url: str
    feed_url: str | None
    requires_credentials: bool
    authority: str
    notes: str = ""


def load_provider_specs(path: Path | None = None) -> list[ProviderSpec]:
    path = path or Path(__file__).resolve().parents[2] / "config" / "providers.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [ProviderSpec(**item) for item in payload["providers"]]


def build_rss_adapters(
    specs: list[ProviderSpec] | None = None,
) -> list[RSSAdapter]:
    """Build only explicitly configured RSS adapters with concrete feed URLs.

    A provider with a catalogue page but no verified feed URL is deliberately
    skipped. This prevents the engine from guessing undocumented endpoints.
    """
    specs = specs or load_provider_specs()
    adapters: list[RSSAdapter] = []
    for spec in specs:
        if not spec.enabled or spec.transport not in {"rss", "rss_catalogue"}:
            continue
        if not spec.feed_url:
            continue
        adapters.append(
            RSSAdapter(
                RSSFeedSpec(
                    provider_id=spec.id,
                    name=spec.name,
                    url=spec.feed_url,
                    tier=spec.tier,
                    category=spec.role[0] if spec.role else "news",
                )
            )
        )
    return adapters


def provider_matrix(specs: list[ProviderSpec] | None = None) -> dict[str, dict[str, Any]]:
    """Return an inspection-friendly matrix for diagnostics and UI layers."""
    specs = specs or load_provider_specs()
    return {
        spec.id: {
            "name": spec.name,
            "tier": spec.tier,
            "enabled": spec.enabled,
            "transport": spec.transport,
            "requires_credentials": spec.requires_credentials,
            "authority": spec.authority,
            "roles": list(spec.role),
        }
        for spec in specs
    }
