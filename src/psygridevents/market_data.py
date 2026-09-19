from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import Iterable

from .market_confirmation import MarketObservation
from .psygrid_client import (
    DEFAULT_BASE_URL,
    FetchDiagnostics,
    MarketSessionStatus,
    PsygridClient,
    SymbolFreshness,
    UniverseCoverageReport,
)


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


class PsygridMarketDataAdapter(MarketDataAdapter):
    """Production adapter: consumes Psygrid's own processed live OHLCV feed.

    Psygrid already owns Dhan authentication, security-ID resolution,
    WebSocket ingestion, reconnect/resubscribe handling and 1-minute candle
    formation. psygridevents does not duplicate any of that -- it only reads
    Psygrid's already-documented, symbol-keyed public HTTP JSON contract:

    - /public/stock/{symbol}.json for the event-driven, single-symbol path
      (CP8 resolves an event to one asset; only that asset is fetched).
    - /public/live.json, once, for the occasional whole-universe coverage
      report (`universe_coverage`) -- never one request per instrument.

    Fields Psygrid genuinely provides -- symbol, timestamp, open, high, low,
    close, volume -- are populated. VWAP, benchmark return and sector return
    are not present in Psygrid's OHLCV contract today (VWAP exists only in
    Psygrid's separate /public/indicators endpoint, not wired in here; see
    docs/LIVE_MARKET_DATA_INTEGRATION.md) and are therefore left as None
    rather than fabricated -- CP10/CP11 already treat these as optional.

    Fails closed: a network failure, non-200 response, malformed JSON, or a
    "NOT_FOUND" symbol all yield an empty observation tuple, exactly like
    NullMarketDataAdapter, so a Psygrid outage degrades signals to
    WATCH/NO_SIGNAL rather than crashing or fabricating a reaction. The
    real-time boundary (never an observation timestamped after `as_of`) and
    malformed-timestamp/candle rejection are enforced inside PsygridClient,
    in addition to CP10's own enforcement.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 10.0,
        client: PsygridClient | None = None,
    ) -> None:
        self._client = client or PsygridClient(base_url, timeout=timeout)
        self.last_diagnostics: dict[str, FetchDiagnostics] = {}

    def observations(self, symbol: str, *, as_of: datetime) -> tuple[MarketObservation, ...]:
        observations, diagnostics = self._client.fetch_stock_observations(symbol, as_of=as_of)
        self.last_diagnostics[symbol] = diagnostics
        return observations

    def freshness(self, symbol: str, *, as_of: datetime, max_age_seconds: float = 120.0) -> SymbolFreshness:
        """How current is this symbol's latest observation, as of `as_of`?

        Mirrors Psygrid's own freshness concept (state_runtime.py), but is
        computed here from the already-fetched observation series rather
        than a separate Psygrid-internal endpoint.
        """
        observations = self.observations(symbol, as_of=as_of)
        if not observations:
            diagnostics = self.last_diagnostics.get(symbol)
            if diagnostics and diagnostics.error:
                return SymbolFreshness(symbol, "UNAVAILABLE", None, None, diagnostics.error)
            return SymbolFreshness(symbol, "NO_DATA", None, None, "no observations available")
        latest = observations[-1]
        age = (as_of - latest.timestamp).total_seconds()
        if age < 0:
            return SymbolFreshness(symbol, "TIME_ERROR", age, latest.timestamp, "latest observation is after as_of")
        status = "LIVE" if age <= max_age_seconds else "STALE"
        return SymbolFreshness(
            symbol, status, round(age, 3), latest.timestamp,
            f"age={round(age, 3)}s vs. threshold={max_age_seconds}s",
        )

    def universe_coverage(
        self, configured_symbols: Iterable[str], *, as_of: datetime, max_age_seconds: float = 120.0
    ) -> UniverseCoverageReport:
        """Runtime coverage of the full configured universe from one /public/live.json fetch."""
        return self._client.universe_coverage(configured_symbols, as_of=as_of, max_age_seconds=max_age_seconds)

    def market_session_status(self) -> MarketSessionStatus:
        """MARKET_OPEN / MARKET_CLOSED / MARKET_DATA_UNAVAILABLE, from Psygrid's own report.

        This is a distinct fact from per-symbol freshness/staleness: it
        answers "is the exchange session Psygrid reports on open at all",
        never "is Psygrid reachable" conflated with "is the market closed".
        """
        return self._client.market_session_status()

    def close(self) -> None:
        self._client.close()
