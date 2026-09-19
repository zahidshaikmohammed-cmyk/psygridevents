from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import httpx

from .market_confirmation import MarketObservation

# Psygrid (zahidshaikmohammed-cmyk/Psygrid) is the canonical owner of live
# market data. It already owns Dhan authentication, security-ID resolution,
# WebSocket ingestion, reconnect handling and 1-minute candle formation
# (config.py, instrument_master.py, dhan_api.py, feed.py, feed_runtime.py,
# session.py, state.py). psygridevents does not reimplement any of that: it
# only reads Psygrid's already-documented, symbol-keyed public HTTP JSON
# contract (output.py::stock_json / market_live_json, served via app.py).
#
# Psygrid's own production deployment target (see its
# .github/workflows/deploy-oracle.yml) is this exact host/port.
DEFAULT_BASE_URL = "http://140.245.226.102:10000"

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class FetchDiagnostics:
    """What happened while fetching one symbol's observations.

    Never surfaced as a fabricated observation -- this is purely for
    coverage/health reporting (see UniverseCoverageReport and
    PsygridMarketDataAdapter.freshness).
    """

    requested_symbol: str
    http_status: int | None
    error: str | None
    raw_candle_count: int
    parsed_count: int
    malformed_count: int
    future_rejected_count: int


@dataclass(frozen=True)
class SymbolFreshness:
    """Mirrors Psygrid's own state_runtime.py::RuntimeFreshnessState.freshness()
    contract (status / data_age_seconds / live_data_valid-equivalent) so
    psygridevents' notion of "live vs stale" matches Psygrid's, rather than
    inventing an unrelated one.
    """

    symbol: str
    status: str  # "LIVE" | "STALE" | "NO_DATA" | "UNAVAILABLE" | "TIME_ERROR"
    data_age_seconds: float | None
    latest_timestamp: datetime | None
    reason: str


@dataclass(frozen=True)
class UniverseCoverageReport:
    """Runtime coverage of the full configured 990-symbol universe.

    Built from a single /public/live.json fetch (not one request per
    instrument), so checking coverage never turns into 990 HTTP calls.
    """

    configured: int
    resolved_security_ids: int
    live_data_received: int
    stale: int
    missing: int
    error: str | None


def parse_ist_timestamp(value: Any) -> datetime | None:
    """Parse Psygrid's "YYYY-MM-DD HH:MM:SS IST" candle/session timestamp.

    Same format Psygrid's own psygrid_master_indicator.py::_parse_ist_timestamp
    and tools/check_live_universe.py parse; reimplemented here without a
    pandas dependency. Returns None (never raises) for anything malformed,
    so a bad timestamp is dropped rather than crashing the fetch.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(" IST"):
        text = text[: -len(" IST")]
    try:
        naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return naive.replace(tzinfo=IST).astimezone(timezone.utc)


class PsygridClient:
    """Thin, read-only HTTP client for Psygrid's public JSON contract.

    Talks only to already-documented, stable endpoints
    (/public/stock/{symbol}.json, /public/live.json, /health). Never
    fabricates a value: every failure mode (network error, non-200,
    malformed JSON, NOT_FOUND symbol, malformed candle) is reported through
    FetchDiagnostics/UniverseCoverageReport rather than silently patched.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        timeout: float = 10.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": "psygridevents-market-data/1.0", "Cache-Control": "no-cache"},
        )

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str) -> tuple[dict[str, Any] | None, int | None, str | None]:
        url = f"{self.base_url}{path}"
        try:
            response = self._client.get(url)
        except httpx.HTTPError as exc:
            return None, None, f"{type(exc).__name__}: {exc}"
        if response.status_code != 200:
            return None, response.status_code, f"HTTP {response.status_code}"
        try:
            payload = response.json()
        except ValueError as exc:
            return None, response.status_code, f"invalid JSON: {exc}"
        if not isinstance(payload, dict):
            return None, response.status_code, "response is not a JSON object"
        return payload, response.status_code, None

    @staticmethod
    def _parse_candles(
        symbol: str, candles: Iterable[Any], *, as_of: datetime
    ) -> tuple[tuple[MarketObservation, ...], int, int]:
        malformed = 0
        future_rejected = 0
        observations: list[MarketObservation] = []
        for candle in candles:
            if not isinstance(candle, dict):
                malformed += 1
                continue
            timestamp = parse_ist_timestamp(candle.get("timestamp"))
            if timestamp is None:
                malformed += 1
                continue
            try:
                open_, high, low, close = (float(candle[key]) for key in ("open", "high", "low", "close"))
                volume = float(candle.get("volume", 0) or 0)
            except (KeyError, TypeError, ValueError):
                malformed += 1
                continue
            if open_ <= 0 or high <= 0 or low <= 0 or close <= 0 or volume < 0:
                malformed += 1
                continue
            if timestamp > as_of:
                future_rejected += 1
                continue
            observations.append(
                MarketObservation(
                    symbol=symbol,
                    timestamp=timestamp,
                    open=open_,
                    high=high,
                    low=low,
                    close=close,
                    volume=volume,
                    vwap=None,
                    average_volume=None,
                    benchmark_return=None,
                    sector_return=None,
                )
            )
        observations.sort(key=lambda item: item.timestamp)
        return tuple(observations), malformed, future_rejected

    def fetch_stock_observations(
        self, symbol: str, *, as_of: datetime
    ) -> tuple[tuple[MarketObservation, ...], FetchDiagnostics]:
        payload, status, error = self._get(f"/public/stock/{symbol}.json")
        if payload is None:
            return (), FetchDiagnostics(symbol, status, error, 0, 0, 0, 0)
        if payload.get("service") != "PSYGRID":
            return (), FetchDiagnostics(symbol, status, "unexpected service in payload", 0, 0, 0, 0)
        if payload.get("status") == "NOT_FOUND":
            return (), FetchDiagnostics(symbol, status, "symbol not found in Psygrid universe", 0, 0, 0, 0)
        if payload.get("status") not in ("OK", None):
            return (), FetchDiagnostics(symbol, status, f"Psygrid status={payload.get('status')}", 0, 0, 0, 0)

        candles = payload.get("candles_1m")
        if not isinstance(candles, list):
            return (), FetchDiagnostics(symbol, status, "malformed candles_1m", 0, 0, 0, 0)

        observations, malformed, future_rejected = self._parse_candles(symbol, candles, as_of=as_of)
        diagnostics = FetchDiagnostics(
            requested_symbol=symbol,
            http_status=status,
            error=None,
            raw_candle_count=len(candles),
            parsed_count=len(observations),
            malformed_count=malformed,
            future_rejected_count=future_rejected,
        )
        return observations, diagnostics

    def fetch_universe_snapshot(self) -> tuple[dict[str, Any] | None, int | None, str | None]:
        return self._get("/public/live.json")

    def universe_coverage(
        self, configured_symbols: Iterable[str], *, as_of: datetime, max_age_seconds: float = 120.0
    ) -> UniverseCoverageReport:
        configured = tuple(configured_symbols)
        payload, _status, error = self.fetch_universe_snapshot()
        if payload is None:
            return UniverseCoverageReport(len(configured), 0, 0, 0, len(configured), error)

        stocks = payload.get("stocks")
        if not isinstance(stocks, dict):
            return UniverseCoverageReport(len(configured), 0, 0, 0, len(configured), "malformed live.json: missing stocks object")

        resolved = live = stale = missing = 0
        for symbol in configured:
            stock = stocks.get(symbol)
            if not isinstance(stock, dict):
                missing += 1
                continue
            if stock.get("security_id"):
                resolved += 1
            candles = stock.get("candles_1m")
            if not isinstance(candles, list) or not candles:
                continue
            latest_ts = parse_ist_timestamp(candles[-1].get("timestamp") if isinstance(candles[-1], dict) else None)
            if latest_ts is None or latest_ts > as_of:
                continue
            age = (as_of - latest_ts).total_seconds()
            if age <= max_age_seconds:
                live += 1
            else:
                stale += 1
        return UniverseCoverageReport(len(configured), resolved, live, stale, missing, None)

    def health(self) -> tuple[bool, str | None]:
        payload, _status, error = self._get("/health")
        if payload is None:
            return False, error
        return payload.get("status") == "OK", None
