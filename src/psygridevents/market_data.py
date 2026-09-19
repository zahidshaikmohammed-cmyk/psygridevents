from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterable

from .market_confirmation import MarketObservation


class MarketDataAdapter(ABC):
    """Live/historical market observation source consumed by CP10.

    An implementation must return only observations that genuinely exist at
    or before `as_of` (the real-time information boundary required by the
    CP11 signal contract). It must never fabricate, interpolate, or forecast
    an observation to fill a gap.
    """

    @abstractmethod
    def observations(self, symbol: str, *, as_of: datetime) -> tuple[MarketObservation, ...]:
        raise NotImplementedError


class NullMarketDataAdapter(MarketDataAdapter):
    """Fail-closed default when no credentialed/live market data source is configured.

    This is intentional, not a stub to be silently swapped later without
    review: per project policy, market observations are never fabricated, so
    an engine wired to this adapter can only ever produce NO_SIGNAL/WATCH on
    the market-response dimension, never a fabricated EARLY_LONG/EARLY_SHORT.
    """

    def observations(self, symbol: str, *, as_of: datetime) -> tuple[MarketObservation, ...]:
        return ()


class StaticMarketDataAdapter(MarketDataAdapter):
    """Serve a pre-supplied set of already-verified observations.

    Used for tests, historical replay validation, and for wiring in
    observations obtained out-of-band from a real feed. The real-time
    boundary is enforced here too: an observation timestamped after `as_of`
    is never returned, even if it was supplied to the constructor.
    """

    def __init__(self, observations: Iterable[MarketObservation]) -> None:
        by_symbol: dict[str, list[MarketObservation]] = {}
        for item in observations:
            by_symbol.setdefault(item.symbol, []).append(item)
        for values in by_symbol.values():
            values.sort(key=lambda item: item.timestamp)
        self._by_symbol = by_symbol

    def observations(self, symbol: str, *, as_of: datetime) -> tuple[MarketObservation, ...]:
        return tuple(item for item in self._by_symbol.get(symbol, ()) if item.timestamp <= as_of)
