"""Bulk PSYGRID market snapshot: all ~989 stocks per minute from the shard endpoints.

One cycle = 22 requests to /public/live-{a..v}.json (or one /public/live.json),
never one request per stock. Each shard is parsed straight into compact
float arrays and the JSON is discarded, so peak memory stays small on an
Oracle Always Free VM.

Everything here is derived from real PSYGRID 1-minute OHLCV. Fields PSYGRID
does not publish (VWAP, relative volume, benchmark/sector returns) are
*computed* from those real candles and labelled as computed; nothing is
interpolated, forward-filled or invented. Candles timestamped after `as_of`,
malformed candles and non-positive prices are rejected.
"""
from __future__ import annotations

import asyncio
import bisect
import logging
import statistics
import time as _time
from array import array
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import httpx

from .market_confirmation import MarketObservation
from .psygrid_client import parse_ist_timestamp

log = logging.getLogger("psygridevents.market")

MARKET_HEALTH_STATES = ("HEALTHY", "DEGRADED", "STALE", "ERROR", "DISCONNECTED", "CLOSED")


@dataclass
class SymbolSeries:
    symbol: str
    security_id: str | None
    previous_close: float | None
    today_open: float | None
    ts: array = field(default_factory=lambda: array("d"))  # bar start, epoch seconds UTC
    open: array = field(default_factory=lambda: array("d"))
    high: array = field(default_factory=lambda: array("d"))
    low: array = field(default_factory=lambda: array("d"))
    close: array = field(default_factory=lambda: array("d"))
    volume: array = field(default_factory=lambda: array("d"))
    malformed: int = 0
    future_rejected: int = 0
    _vwap: array | None = None

    def __len__(self) -> int:
        return len(self.ts)

    @property
    def latest_ts(self) -> float | None:
        return self.ts[-1] if self.ts else None

    def index_at_or_before(self, epoch: float) -> int:
        """Index of the last bar whose start is <= epoch, or -1."""
        return bisect.bisect_right(self.ts, epoch) - 1

    def close_at(self, epoch: float) -> float | None:
        index = self.index_at_or_before(epoch)
        return self.close[index] if index >= 0 else None

    def vwap_series(self) -> array:
        """Session VWAP per bar from typical price x volume (computed from real candles)."""
        if self._vwap is not None and len(self._vwap) == len(self.ts):
            return self._vwap
        result = array("d")
        cumulative_pv = 0.0
        cumulative_v = 0.0
        for index in range(len(self.ts)):
            typical = (self.high[index] + self.low[index] + self.close[index]) / 3.0
            volume = self.volume[index]
            cumulative_pv += typical * volume
            cumulative_v += volume
            result.append(cumulative_pv / cumulative_v if cumulative_v > 0 else float("nan"))
        self._vwap = result
        return result


@dataclass
class IndexSeries:
    route: str
    ts: array = field(default_factory=lambda: array("d"))
    close: array = field(default_factory=lambda: array("d"))
    ltp: float | None = None

    def close_at(self, epoch: float) -> float | None:
        index = bisect.bisect_right(self.ts, epoch) - 1
        return self.close[index] if index >= 0 else None


@dataclass
class MarketSnapshot:
    fetched_at: datetime
    as_of: datetime
    psygrid_status: str | None
    session_status: str | None
    psygrid_clock: datetime | None
    series: dict[str, SymbolSeries]
    errors: list[str]
    shards_ok: int
    shards_total: int
    fetch_seconds: float
    max_data_age_seconds: float
    benchmark: IndexSeries | None = None
    vix: float | None = None
    sector_of: dict[str, str] = field(default_factory=dict)
    _median_cache: dict[tuple, float | None] = field(default_factory=dict)

    # ------------------------------------------------------------- freshness
    @property
    def latest_bar_epoch(self) -> float | None:
        values = [item.latest_ts for item in self.series.values() if item.latest_ts is not None]
        return max(values) if values else None

    def data_age_seconds(self, symbol: str | None = None) -> float | None:
        epoch = self.series[symbol].latest_ts if symbol and symbol in self.series else self.latest_bar_epoch
        if epoch is None:
            return None
        # A 1-minute candle is complete at bar start + 60s.
        return round(self.as_of.timestamp() - (epoch + 60.0), 3)

    def symbol_freshness(self, symbol: str) -> str:
        series = self.series.get(symbol)
        if series is None or not len(series):
            return "NO_DATA"
        age = self.data_age_seconds(symbol)
        if age is None:
            return "NO_DATA"
        if age < -90:
            return "TIME_ERROR"
        return "LIVE" if age <= self.max_data_age_seconds else "STALE"

    def coverage(self) -> dict[str, int]:
        cached = self._median_cache.get(("coverage",))
        if cached is not None:
            return dict(cached)  # type: ignore[arg-type]
        live = stale = no_data = zero_volume = 0
        for symbol, series in self.series.items():
            state = self.symbol_freshness(symbol)
            if state == "LIVE":
                live += 1
            elif state == "NO_DATA":
                no_data += 1
            else:
                stale += 1
            if len(series) >= 5 and sum(series.volume[-5:]) == 0:
                zero_volume += 1
        result = {"symbols": len(self.series), "live": live, "stale": stale, "no_data": no_data,
                  "zero_volume_last5": zero_volume}
        self._median_cache[("coverage",)] = result  # type: ignore[assignment]
        return dict(result)

    def health(self) -> str:
        if self.shards_ok == 0:
            return "DISCONNECTED"
        if self.psygrid_status == "CLOSED" or self.session_status == "CLOSED":
            return "CLOSED"
        if self.psygrid_status not in ("OK", None):
            return "ERROR"
        cov = self.coverage()
        if cov["symbols"] == 0 or cov["live"] == 0:
            return "STALE"
        if self.shards_ok < self.shards_total or cov["live"] < 0.8 * cov["symbols"]:
            return "DEGRADED"
        return "HEALTHY"

    # ---------------------------------------------------------- cross-section
    def _median_return(self, symbols: Iterable[str], start: float, end: float, key: tuple) -> float | None:
        if key in self._median_cache:
            return self._median_cache[key]
        values = []
        for symbol in symbols:
            series = self.series.get(symbol)
            if series is None or not len(series):
                continue
            base = series.close_at(start)
            last = series.close_at(end)
            if base and last and base > 0:
                values.append(last / base - 1.0)
        result = statistics.median(values) if len(values) >= 5 else None
        self._median_cache[key] = result
        return result

    def universe_return(self, start: float, end: float) -> float | None:
        return self._median_return(self.series.keys(), start, end, ("universe", start, end))

    def sector_return(self, sector: str, start: float, end: float, exclude: str | None = None) -> float | None:
        members = [symbol for symbol, value in self.sector_of.items() if value == sector and symbol != exclude]
        return self._median_return(members, start, end, ("sector", sector, exclude, start, end))

    def benchmark_return(self, start: float, end: float) -> tuple[float | None, str]:
        if self.benchmark is not None and len(self.benchmark.ts):
            base = self.benchmark.close_at(start)
            last = self.benchmark.close_at(end)
            if base and last and base > 0:
                return last / base - 1.0, f"index:{self.benchmark.route}"
        value = self.universe_return(start, end)
        return value, "universe_median"

    def breadth(self) -> dict[str, Any]:
        advancing = declining = 0
        for series in self.series.values():
            if not len(series) or not series.previous_close:
                continue
            change = series.close[-1] / series.previous_close - 1.0
            if change > 0:
                advancing += 1
            elif change < 0:
                declining += 1
        total = advancing + declining
        return {
            "advancing": advancing,
            "declining": declining,
            "advance_ratio": round(advancing / total, 4) if total else None,
        }

    def regime(self) -> dict[str, Any]:
        """Simple, transparent market regime from breadth, benchmark day move and VIX."""
        breadth = self.breadth()
        day_move = None
        basis = None
        if self.benchmark is not None and len(self.benchmark.close) >= 2:
            day_move = self.benchmark.close[-1] / self.benchmark.close[0] - 1.0
            basis = f"index:{self.benchmark.route}(since first bar)"
        ratio = breadth.get("advance_ratio")
        label = "UNKNOWN"
        if ratio is not None:
            if ratio >= 0.65 and (day_move is None or day_move > 0):
                label = "RISK_ON"
            elif ratio <= 0.35 and (day_move is None or day_move < 0):
                label = "RISK_OFF"
            else:
                label = "MIXED"
        return {"label": label, "breadth": breadth, "benchmark_day_move": day_move, "benchmark_basis": basis,
                "india_vix": self.vix}

    # ------------------------------------------------------- legacy adapters
    def observations(
        self, symbol: str, *, anchor_epoch: float | None = None, baseline_bars: int = 20, relative_window: int = 60,
    ) -> tuple[MarketObservation, ...]:
        """MarketObservation tuple for the CP5/CP10 engines, enriched with computed fields.

        vwap            -- session VWAP computed from PSYGRID candles
        average_volume  -- median volume of up to `baseline_bars` prior bars (>= 5 required)
        benchmark_return/sector_return -- returns since `anchor_epoch` (the event baseline),
                           so CP5/CP10 relative-performance arithmetic is like-for-like.
        """
        series = self.series.get(symbol)
        if series is None:
            return ()
        vwap = series.vwap_series()
        sector = self.sector_of.get(symbol)
        result: list[MarketObservation] = []
        for index in range(len(series)):
            prior = series.volume[max(0, index - baseline_bars):index]
            average_volume = statistics.median(prior) if len(prior) >= 5 and statistics.median(prior) > 0 else None
            bench = sector_ret = None
            if anchor_epoch is not None and series.ts[index] > anchor_epoch and index >= len(series) - relative_window:
                bench, _ = self.benchmark_return(anchor_epoch, series.ts[index])
                if sector:
                    sector_ret = self.sector_return(sector, anchor_epoch, series.ts[index], exclude=symbol)
            value = vwap[index]
            result.append(
                MarketObservation(
                    symbol=symbol,
                    timestamp=datetime.fromtimestamp(series.ts[index], tz=timezone.utc),
                    open=series.open[index], high=series.high[index], low=series.low[index],
                    close=series.close[index], volume=series.volume[index],
                    vwap=None if value != value else value,
                    average_volume=average_volume, benchmark_return=bench, sector_return=sector_ret,
                )
            )
        return tuple(result)


_TS_CACHE: dict[str, float | None] = {}


def _epoch(value: Any) -> float | None:
    """PSYGRID "YYYY-MM-DD HH:MM:SS IST" -> epoch seconds. All ~989 stocks share the same
    minute stamps, so results are cached (bounded) instead of re-parsing ~370k strings a minute."""
    if not isinstance(value, str):
        return None
    cached = _TS_CACHE.get(value, False)
    if cached is not False:
        return cached  # type: ignore[return-value]
    parsed = parse_ist_timestamp(value)
    result = parsed.timestamp() if parsed is not None else None
    if len(_TS_CACHE) > 20000:
        _TS_CACHE.clear()
    _TS_CACHE[value] = result
    return result


def parse_stock_payload(symbol: str, stock: dict[str, Any], *, as_of: datetime) -> SymbolSeries:
    def number(value: Any) -> float | None:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return None
        return result if result > 0 else None

    series = SymbolSeries(
        symbol=symbol,
        security_id=str(stock.get("security_id")) if stock.get("security_id") else None,
        previous_close=number(stock.get("previous_close")),
        today_open=number(stock.get("today_open")),
    )
    candles = stock.get("candles_1m")
    if not isinstance(candles, list):
        return series
    limit = as_of.timestamp()
    last_ts = -1.0
    for candle in candles:
        if not isinstance(candle, dict):
            series.malformed += 1
            continue
        epoch = _epoch(candle.get("timestamp"))
        if epoch is None:
            series.malformed += 1
            continue
        try:
            o, h, low, c = (float(candle[key]) for key in ("open", "high", "low", "close"))
            v = float(candle.get("volume", 0) or 0)
        except (KeyError, TypeError, ValueError):
            series.malformed += 1
            continue
        if min(o, h, low, c) <= 0 or v < 0 or h < low:
            series.malformed += 1
            continue
        if epoch > limit:
            series.future_rejected += 1
            continue
        if epoch <= last_ts:  # PSYGRID de-duplicates by minute; keep strictly increasing
            continue
        last_ts = epoch
        series.ts.append(epoch)
        series.open.append(o)
        series.high.append(h)
        series.low.append(low)
        series.close.append(c)
        series.volume.append(v)
    return series


class PsygridBulkClient:
    """Async bulk reader of PSYGRID's public JSON contract (no Dhan access, no second feed)."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        fetch_mode: str = "shards",
        shard_letters: str = "abcdefghijklmnopqrstuv",
        max_data_age_seconds: float = 150.0,
        benchmark_route: str | None = "nifty",
        vix_route: str | None = "indiavix",
        fetch_sectors: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
        fallback_sector_of: dict[str, str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.fetch_mode = fetch_mode
        self.shard_letters = shard_letters
        self.max_data_age_seconds = max_data_age_seconds
        self.benchmark_route = benchmark_route
        self.vix_route = vix_route
        self.fetch_sectors = fetch_sectors
        self.fallback_sector_of = dict(fallback_sector_of or {})
        self._sector_of: dict[str, str] = dict(self.fallback_sector_of)
        self._sector_refreshed: float | None = None
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            headers={"User-Agent": "psygridevents-bulk/2.0", "Cache-Control": "no-cache", "Accept-Encoding": "gzip"},
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _get_json(self, path: str) -> tuple[dict[str, Any] | None, str | None]:
        try:
            response = await self._client.get(f"{self.base_url}{path}")
        except httpx.HTTPError as exc:
            return None, f"{path}: {type(exc).__name__}: {exc}"
        if response.status_code != 200:
            return None, f"{path}: HTTP {response.status_code}"
        try:
            payload = response.json()
        except ValueError as exc:
            return None, f"{path}: invalid JSON ({exc})"
        if not isinstance(payload, dict):
            return None, f"{path}: not a JSON object"
        return payload, None

    async def health(self) -> tuple[bool, str | None]:
        payload, error = await self._get_json("/health")
        if payload is None:
            return False, error
        return str(payload.get("status", "")).upper() in {"OK", "HEALTHY", "LIVE"}, None

    async def snapshot(self, *, as_of: datetime | None = None, universe: Iterable[str] | None = None) -> MarketSnapshot:
        started = _time.monotonic()
        as_of = as_of or datetime.now(timezone.utc)
        paths = (
            [f"/public/live-{letter}.json" for letter in self.shard_letters]
            if self.fetch_mode == "shards" else ["/public/live.json"]
        )
        wanted = set(universe) if universe is not None else None
        series: dict[str, SymbolSeries] = {}
        errors: list[str] = []
        ok = 0
        status: str | None = None
        session_status: str | None = None
        clock: datetime | None = None

        # Fetch shards with bounded concurrency; parse each and drop its JSON immediately.
        semaphore = asyncio.Semaphore(4)

        async def load(path: str) -> None:
            nonlocal ok, status, session_status, clock
            async with semaphore:
                payload, error = await self._get_json(path)
            if payload is None:
                errors.append(error or f"{path}: unknown error")
                return
            if payload.get("service") not in (None, "PSYGRID"):
                errors.append(f"{path}: unexpected service {payload.get('service')!r}")
                return
            ok += 1
            status = status or payload.get("status")
            session = payload.get("session") if isinstance(payload.get("session"), dict) else {}
            session_status = session_status or session.get("status")
            clock = clock or parse_ist_timestamp(session.get("current_time_ist"))
            stocks = payload.get("stocks")
            if not isinstance(stocks, dict):
                errors.append(f"{path}: missing stocks object")
                return
            for symbol, stock in stocks.items():
                symbol = str(symbol).upper()
                if wanted is not None and symbol not in wanted:
                    continue
                if isinstance(stock, dict):
                    series[symbol] = parse_stock_payload(symbol, stock, as_of=as_of)

        await asyncio.gather(*(load(path) for path in paths))

        benchmark = None
        vix = None
        if self.benchmark_route:
            payload, error = await self._get_json(f"/public/{self.benchmark_route}.json")
            if payload is not None and isinstance(payload.get("1m"), list):
                benchmark = IndexSeries(route=self.benchmark_route)
                for candle in payload["1m"]:
                    stamp = parse_ist_timestamp(candle.get("timestamp")) if isinstance(candle, dict) else None
                    try:
                        close = float(candle.get("close"))
                    except (AttributeError, TypeError, ValueError):
                        continue
                    if stamp is None or close <= 0 or stamp > as_of:
                        continue
                    if benchmark.ts and stamp.timestamp() <= benchmark.ts[-1]:
                        continue
                    benchmark.ts.append(stamp.timestamp())
                    benchmark.close.append(close)
            elif error:
                errors.append(error)
        if self.vix_route:
            payload, _ = await self._get_json(f"/public/{self.vix_route}.json")
            if payload is not None:
                try:
                    vix = float(payload.get("ltp")) if payload.get("ltp") is not None else None
                except (TypeError, ValueError):
                    vix = None

        if self.fetch_sectors and (self._sector_refreshed is None or _time.monotonic() - self._sector_refreshed > 3600):
            payload, _ = await self._get_json("/public/sectors.json")
            if payload is not None and isinstance(payload.get("sectors"), list):
                refreshed: dict[str, str] = dict(self.fallback_sector_of)
                for sector in payload["sectors"]:
                    name = sector.get("sector") if isinstance(sector, dict) else None
                    if not name or name == "OTHER":
                        continue
                    for member in sector.get("constituents", []) or []:
                        if isinstance(member, dict) and member.get("symbol"):
                            refreshed[str(member["symbol"]).upper()] = str(name)
                self._sector_of = refreshed
                self._sector_refreshed = _time.monotonic()

        return MarketSnapshot(
            fetched_at=datetime.now(timezone.utc), as_of=as_of, psygrid_status=status, session_status=session_status,
            psygrid_clock=clock, series=series, errors=errors, shards_ok=ok, shards_total=len(paths),
            fetch_seconds=round(_time.monotonic() - started, 3), max_data_age_seconds=self.max_data_age_seconds,
            benchmark=benchmark, vix=vix, sector_of=dict(self._sector_of),
        )


def volume_profile(series: SymbolSeries, *, bucket_minutes: int = 15, market_open_epoch: float) -> tuple[dict[str, float], float, int]:
    """Cumulative volume at the end of each `bucket_minutes` bucket since the open (for future RVOL)."""
    buckets: dict[str, float] = {}
    cumulative = 0.0
    for index in range(len(series)):
        cumulative += series.volume[index]
        minute = int((series.ts[index] - market_open_epoch) // 60)
        if minute < 0:
            continue
        bucket = (minute // bucket_minutes + 1) * bucket_minutes
        buckets[str(bucket)] = cumulative
    return buckets, cumulative, len(series)
