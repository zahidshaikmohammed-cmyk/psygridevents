"""Load the production source matrix (config/sources.yaml) into adapters + health trackers."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .adapters import build_adapter
from .base import (
    Activation,
    FetchResult,
    RobotsDisallowed,
    SourceAdapter,
    SourceError,
    SourceQuality,
    SourceSpec,
)
from .health import HealthState, ProviderHealthTracker, ProviderStatus
from .http import SourceHttpClient

log = logging.getLogger("psygridevents.providers")

_SPEC_FIELDS = set(SourceSpec.__dataclass_fields__)


def load_source_specs(path: Path) -> list[SourceSpec]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    defaults = payload.get("defaults", {}) or {}
    specs: list[SourceSpec] = []
    seen: set[str] = set()
    for item in payload.get("sources", []) or []:
        merged = {**defaults, **item}
        unknown = set(merged) - _SPEC_FIELDS
        if unknown:
            raise ValueError(f"source {merged.get('id')!r} has unknown fields {sorted(unknown)}")
        if merged["id"] in seen:
            raise ValueError(f"duplicate source id {merged['id']!r}")
        seen.add(merged["id"])
        merged["quality"] = SourceQuality(merged["quality"])
        merged["activation"] = Activation(merged.get("activation", "auto"))
        merged["queries"] = tuple(merged.get("queries") or ())
        merged["params"] = dict(merged.get("params") or {})
        spec = SourceSpec(**merged)
        if spec.kind != "unavailable" and spec.activation != Activation.UNAVAILABLE:
            if spec.kind in {"rss", "html_links", "nse_deals_csv"} and not (spec.url or spec.catalogue_url):
                raise ValueError(f"source {spec.id!r} needs a documented url or catalogue_url")
            if spec.kind in {"google_news", "gdelt"} and not spec.queries:
                raise ValueError(f"source {spec.id!r} needs queries")
        specs.append(spec)
    return specs


@dataclass
class ManagedSource:
    spec: SourceSpec
    adapter: SourceAdapter
    tracker: ProviderHealthTracker
    lock: asyncio.Lock


class SourceRegistry:
    """Owns every source, its probe/poll schedule and its health."""

    def __init__(self, specs: list[SourceSpec], *, persisted: dict[str, dict[str, Any]] | None = None) -> None:
        self.sources: dict[str, ManagedSource] = {}
        persisted = persisted or {}
        for spec in specs:
            status = ProviderStatus(
                provider_id=spec.id, name=spec.name, kind=spec.kind, quality=spec.quality.value,
                category=spec.category, activation=spec.activation.value, verification=spec.verification,
                resolved_url=spec.url,
            )
            previous = persisted.get(spec.id)
            if previous:
                restored = ProviderStatus.from_dict(previous)
                # Counters/history survive restarts; activation is re-earned by a fresh probe.
                status.total_attempts = restored.total_attempts
                status.total_failures = restored.total_failures
                status.total_observations = restored.total_observations
                status.last_success_at = restored.last_success_at
                status.newest_item_at = restored.newest_item_at
            tracker = ProviderHealthTracker(status, poll_seconds=spec.poll_seconds, stale_after_hours=spec.stale_after_hours)
            adapter = build_adapter(spec)
            if spec.kind == "unavailable" or spec.activation == Activation.UNAVAILABLE:
                tracker.mark_unavailable(spec.notes or "no documented public machine-readable endpoint")
            elif spec.activation == Activation.DISABLED:
                tracker.mark_unavailable("disabled by configuration")
            self.sources[spec.id] = ManagedSource(spec, adapter, tracker, asyncio.Lock())

    def statuses(self) -> list[ProviderStatus]:
        return [item.tracker.status for item in self.sources.values()]

    def needs_probe(self, item: ManagedSource, now: datetime) -> bool:
        status = item.tracker.status
        if status.state == HealthState.UNAVAILABLE.value and status.last_probe_at is None and (
            item.spec.kind == "unavailable" or item.spec.activation != Activation.AUTO
        ):
            return False
        if item.spec.activation != Activation.AUTO or item.spec.kind == "unavailable":
            return False
        if status.last_probe_ok:
            return False
        return status.next_due_at is None or now >= status.next_due_at

    async def probe(self, item: ManagedSource, http: SourceHttpClient, now: datetime) -> bool:
        async with item.lock:
            try:
                result = await item.adapter.probe(http, now=now)
            except RobotsDisallowed as exc:
                item.tracker.mark_unavailable(str(exc))
                item.tracker.status.last_probe_at = now
                item.tracker.status.last_probe_ok = False
                log.warning("source %s disallowed by robots.txt", item.spec.id)
                return False
            except (SourceError, Exception) as exc:  # noqa: BLE001 - one provider never stops the engine
                item.tracker.record_probe(False, now, error=f"{type(exc).__name__}: {exc}")
                log.warning("source %s probe failed: %s", item.spec.id, exc)
                return False
            item.tracker.record_probe(True, now, url=item.adapter.resolved_url, notes=result.notes)
            log.info("source %s probe ok (%s items) url=%s", item.spec.id, result.items_seen, item.adapter.resolved_url)
            return True

    async def poll(
        self, item: ManagedSource, http: SourceHttpClient, *, since: datetime | None, now: datetime,
        interval_seconds: float,
    ) -> FetchResult | None:
        async with item.lock:
            try:
                result = await item.adapter.fetch(http, since=since, now=now)
            except RobotsDisallowed as exc:
                item.tracker.mark_unavailable(str(exc))
                return None
            except (SourceError, Exception) as exc:  # noqa: BLE001
                item.tracker.record_failure(now, f"{type(exc).__name__}: {exc}", interval_seconds=interval_seconds)
                log.warning("source %s fetch failed (%s consecutive): %s", item.spec.id,
                            item.tracker.status.consecutive_failures, exc)
                return None
            item.tracker.record_success(
                now, observations=len(result.observations), latency_ms=result.latency_ms,
                newest_item_at=result.newest_item_at, interval_seconds=interval_seconds,
            )
            return result
