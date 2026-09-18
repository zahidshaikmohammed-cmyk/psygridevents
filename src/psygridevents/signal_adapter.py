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


class HttpMarketDataAdapter:
    """Adapter for an externally supplied real-market JSON observation endpoint."""

    def __init__(self, url: str, *, timeout_seconds: float = 10.0) -> None:
        self.url = url
        self.timeout_seconds = timeout_seconds

    @classmethod
    def from_environment(cls):
        import os
        url = os.getenv("PSYGRID_MARKET_DATA_URL")
        if not url:
            return NullMarketDataAdapter()
        return cls(url, timeout_seconds=float(os.getenv("PSYGRID_MARKET_DATA_TIMEOUT", "10")))

    def observations(self, symbols, start, end):
        import httpx
        from datetime import datetime, timezone
        try:
            response = httpx.get(self.url, timeout=self.timeout_seconds, params={"symbols": ",".join(sorted(set(symbols))), "start": start.astimezone(timezone.utc).isoformat(), "end": end.astimezone(timezone.utc).isoformat()})
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("observations", []) if isinstance(payload, dict) else payload
            if not isinstance(rows, list): return ()
            result = []
            for row in rows:
                try:
                    result.append(MarketObservation(symbol=str(row["symbol"]), timestamp=datetime.fromisoformat(str(row["timestamp"]).replace("Z", "+00:00")), open=float(row["open"]), high=float(row["high"]), low=float(row["low"]), close=float(row["close"]), volume=float(row["volume"]), vwap=None if row.get("vwap") is None else float(row["vwap"]), average_volume=None if row.get("average_volume") is None else float(row["average_volume"]), benchmark_return=None if row.get("benchmark_return") is None else float(row["benchmark_return"]), sector_return=None if row.get("sector_return") is None else float(row["sector_return"])))
                except (KeyError, TypeError, ValueError):
                    continue
            return tuple(sorted(result, key=lambda item: (item.timestamp, item.symbol)))
        except (httpx.HTTPError, ValueError, TypeError):
            return ()
