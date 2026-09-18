from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from .issuer_master import IssuerMasterBuilder, IssuerRecord


class IssuerMasterRuntime:
    """Load a verified issuer master from cache, refreshing from official NSE CSV when stale."""

    def __init__(
        self,
        universe_file: str | Path,
        cache_file: str | Path,
        refresh_url: str,
        *,
        max_age_hours: float = 24.0,
        timeout: float = 15.0,
    ) -> None:
        self.universe_file = Path(universe_file)
        self.cache_file = Path(cache_file)
        self.refresh_url = refresh_url
        self.max_age_hours = max_age_hours
        self.timeout = timeout

    def load(self) -> tuple[IssuerRecord, ...]:
        cached = self._load_cache()
        if cached and not self._stale():
            return cached
        try:
            builder = IssuerMasterBuilder(self.universe_file)
            rows = builder.fetch_csv(self.refresh_url, timeout=self.timeout)
            records = tuple(builder.merge(rows))
            self._save_cache(records)
            return records
        except Exception:
            # A stale verified cache is preferable to guessing. If no cache exists,
            # return an empty set and let entity resolution fail closed.
            return cached

    def _stale(self) -> bool:
        if not self.cache_file.exists():
            return True
        age = datetime.now(timezone.utc).timestamp() - self.cache_file.stat().st_mtime
        return age > self.max_age_hours * 3600

    def _load_cache(self) -> tuple[IssuerRecord, ...]:
        if not self.cache_file.exists():
            return ()
        try:
            payload = json.loads(self.cache_file.read_text(encoding="utf-8"))
            return tuple(IssuerRecord(**item) for item in payload.get("records", []))
        except (OSError, ValueError, TypeError, KeyError):
            return ()

    def _save_cache(self, records: tuple[IssuerRecord, ...]) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": "1.0.0",
            "source": self.refresh_url,
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
            "records": [record.__dict__ for record in records],
        }
        self.cache_file.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
