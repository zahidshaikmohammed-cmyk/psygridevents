from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any


class SourceAdapter(ABC):
    """Contract for a news, announcement, calendar or public-data source."""

    name: str
    source_tier: int

    @abstractmethod
    def fetch(self, since: datetime | None = None) -> list[dict[str, Any]]:
        """Return raw source records without inventing or rewriting facts."""
        raise NotImplementedError


class NullSource(SourceAdapter):
    """Safe placeholder used until real adapters are configured."""

    name = "null"
    source_tier = 5

    def fetch(self, since: datetime | None = None) -> list[dict[str, Any]]:
        return []
