from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATE_SCHEMA_VERSION = "1.0"

DEFAULT_STATE_PATH = Path(".psygridevents_state.json")


@dataclass(frozen=True)
class SignalPublicationRecord:
    event_id: str
    signal_state: str
    last_published_at: datetime


class PublicationStateStore:
    """The smallest production-safe persistence psygridevents needs.

    A single local JSON file (no database), tracking exactly two things:

    - the acquisition watermark (`since`), so a restarted process resumes
      incremental acquisition instead of silently re-processing everything
      it already saw;
    - the last-published signal_state per event_id, so a restarted process
      does not re-announce a signal as "NEW" when it was already published
      before the restart.

    Writes are atomic (write to a temp file, then rename) so a crash mid-save
    can never leave a half-written, corrupt state file in place. A state file
    that fails to parse is treated as absent (fail-safe empty state) rather
    than raising -- losing the watermark degrades to "reprocess a bit more
    than necessary", never a crash and never fabricated state.
    """

    def __init__(self, path: str | Path = DEFAULT_STATE_PATH) -> None:
        self.path = Path(path)
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"schema_version": STATE_SCHEMA_VERSION, "since": None, "signals": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"schema_version": STATE_SCHEMA_VERSION, "since": None, "signals": {}}
        if not isinstance(data, dict) or not isinstance(data.get("signals"), dict):
            return {"schema_version": STATE_SCHEMA_VERSION, "since": None, "signals": {}}
        return data

    def since(self) -> datetime | None:
        value = self._data.get("since")
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    def last_signal_state(self, event_id: str) -> str | None:
        record = self._data.get("signals", {}).get(event_id)
        if not isinstance(record, dict):
            return None
        return record.get("signal_state")

    def known_event_ids(self) -> frozenset[str]:
        return frozenset(self._data.get("signals", {}).keys())

    def record_signal(self, event_id: str, signal_state: str, *, as_of: datetime) -> None:
        # Idempotent: writing the same (event_id, signal_state) twice leaves
        # the stored record's content unchanged (only last_published_at
        # advances), and re-running with the same inputs is safe.
        self._data.setdefault("signals", {})[event_id] = {
            "signal_state": signal_state,
            "last_published_at": as_of.isoformat(),
        }

    def record_provider_attempt(
        self, provider_id: str, *, success: bool, at: datetime, error: str | None = None
    ) -> None:
        """Durable provider health, across restarts -- diagnostic only.

        `last_success_at` only ever advances on an actual success; a failed
        attempt updates `last_attempt_at`/`last_error` but never erases the
        last known-good timestamp.
        """
        providers = self._data.setdefault("providers", {})
        existing = providers.get(provider_id, {})
        providers[provider_id] = {
            "last_attempt_at": at.isoformat(),
            "last_success_at": at.isoformat() if success else existing.get("last_success_at"),
            "last_error": None if success else (error or "unknown error"),
        }

    def provider_status(self, provider_id: str) -> dict | None:
        return self._data.get("providers", {}).get(provider_id)

    def known_provider_ids(self) -> frozenset[str]:
        return frozenset(self._data.get("providers", {}).keys())

    def advance_since(self, as_of: datetime) -> None:
        # Never move the watermark backward or into the future relative to
        # what the caller has actually observed -- the caller always passes
        # the `as_of` it just used to run the pipeline, never a guess.
        current = self.since()
        if current is not None and as_of < current:
            return
        self._data["since"] = as_of.isoformat()

    def save(self) -> None:
        self._data["schema_version"] = STATE_SCHEMA_VERSION
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(self.path)

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
