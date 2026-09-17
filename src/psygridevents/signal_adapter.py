from __future__ import annotations

from datetime import datetime
from typing import Iterable, Protocol

from .market_confirmation import MarketObservation


class MarketDataAdapter(Protocol):
    """Live market-data boundary. Implementations must supply real observations only."""

    def observations(self, symbols: Iterable[str], start: datetime, end: datetime) -> tuple[MarketObservation, ...]:
        ...


class NullMarketDataAdapter:
    """Explicitly unavailable adapter used until a real entitled provider is configured."""

    def observations(self, symbols: Iterable[str], start: datetime, end: datetime) -> tuple[MarketObservation, ...]:
        return ()
