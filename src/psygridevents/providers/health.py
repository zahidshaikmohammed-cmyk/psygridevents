"""Per-provider health state machine with backoff. One failed provider never stops the engine."""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any


class HealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    ERROR = "ERROR"
    DISCONNECTED = "DISCONNECTED"
    UNAVAILABLE = "UNAVAILABLE"  # no documented endpoint / disabled / robots-disallowed
    PENDING = "PENDING"  # configured, not yet probed


DISCONNECT_AFTER_FAILURES = 3
MAX_BACKOFF_SECONDS = 1800.0
REPROBE_SECONDS = 6 * 3600.0


@dataclass
class ProviderStatus:
    provider_id: str
    name: str
    kind: str
    quality: str
    category: str
    activation: str
    verification: str
    state: str = HealthState.PENDING.value
    active: bool = False
    resolved_url: str | None = None
    last_probe_at: datetime | None = None
    last_probe_ok: bool | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    consecutive_failures: int = 0
    total_attempts: int = 0
    total_failures: int = 0
    total_observations: int = 0
    last_observation_count: int = 0
    last_latency_ms: float | None = None
    newest_item_at: datetime | None = None
    next_due_at: datetime | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key, value in list(data.items()):
            if isinstance(value, datetime):
                data[key] = value.isoformat()
        return data

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ProviderStatus":
        known = {name for name in cls.__dataclass_fields__}
        values = {key: value for key, value in payload.items() if key in known}
        for key in ("last_probe_at", "last_attempt_at", "last_success_at", "newest_item_at", "next_due_at"):
            if values.get(key):
                try:
                    values[key] = datetime.fromisoformat(values[key])
                except (TypeError, ValueError):
                    values[key] = None
        return cls(**values)


class ProviderHealthTracker:
    def __init__(self, status: ProviderStatus, *, poll_seconds: float, stale_after_hours: float | None) -> None:
        self.status = status
        self.poll_seconds = poll_seconds
        self.stale_after = timedelta(hours=stale_after_hours) if stale_after_hours else None

    def mark_unavailable(self, reason: str) -> None:
        self.status.state = HealthState.UNAVAILABLE.value
        self.status.active = False
        self.status.last_error = reason[:500]

    def record_probe(self, ok: bool, now: datetime, *, error: str | None = None, url: str | None = None,
                     notes: tuple[str, ...] = ()) -> None:
        self.status.last_probe_at = now
        self.status.last_probe_ok = ok
        if url:
            self.status.resolved_url = url
        self.status.notes = list(notes)[:10]
        if ok:
            self.status.active = True
            self.status.state = HealthState.HEALTHY.value
            self.status.last_error = None
            self.status.next_due_at = now
        else:
            self.status.active = False
            self.status.state = HealthState.DISCONNECTED.value
            self.status.last_error = (error or "probe failed")[:500]
            self.status.next_due_at = now + timedelta(seconds=REPROBE_SECONDS)

    def record_success(self, now: datetime, *, observations: int, latency_ms: float | None,
                       newest_item_at: datetime | None, interval_seconds: float) -> None:
        status = self.status
        status.last_attempt_at = now
        status.last_success_at = now
        status.total_attempts += 1
        status.total_observations += observations
        status.last_observation_count = observations
        status.last_latency_ms = latency_ms
        status.consecutive_failures = 0
        status.last_error = None
        if newest_item_at and (status.newest_item_at is None or newest_item_at > status.newest_item_at):
            status.newest_item_at = newest_item_at
        status.state = self._state_after_success(now).value
        status.next_due_at = now + timedelta(seconds=interval_seconds * random.uniform(0.95, 1.05))

    def record_failure(self, now: datetime, error: str, *, interval_seconds: float) -> None:
        status = self.status
        status.last_attempt_at = now
        status.total_attempts += 1
        status.total_failures += 1
        status.consecutive_failures += 1
        status.last_error = error[:500]
        if status.consecutive_failures >= DISCONNECT_AFTER_FAILURES:
            status.state = HealthState.DISCONNECTED.value
        else:
            status.state = HealthState.ERROR.value
        backoff = min(MAX_BACKOFF_SECONDS, interval_seconds * (2 ** min(status.consecutive_failures, 6)))
        status.next_due_at = now + timedelta(seconds=backoff * random.uniform(0.9, 1.1))

    def _state_after_success(self, now: datetime) -> HealthState:
        recent_failure_rate = self.status.total_failures / max(1, self.status.total_attempts)
        if self.stale_after and self.status.newest_item_at and now - self.status.newest_item_at > self.stale_after:
            return HealthState.STALE
        if recent_failure_rate > 0.5 and self.status.total_attempts >= 4:
            return HealthState.DEGRADED
        return HealthState.HEALTHY

    def refresh_staleness(self, now: datetime) -> None:
        if self.status.state in (HealthState.HEALTHY.value, HealthState.STALE.value, HealthState.DEGRADED.value):
            self.status.state = self._state_after_success(now).value

    def is_due(self, now: datetime) -> bool:
        return self.status.next_due_at is None or now >= self.status.next_due_at


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
