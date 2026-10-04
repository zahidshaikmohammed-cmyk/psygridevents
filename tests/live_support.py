"""Deterministic fixtures for the production engine tests (no network, no wall clock)."""
from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from psygridevents.acquisition import RawObservation
from psygridevents.live_engine import LiveEngine
from psygridevents.market_snapshot import IndexSeries, MarketSnapshot, SymbolSeries
from psygridevents.settings import load_settings
from psygridevents.storage import Store

IST = ZoneInfo("Asia/Kolkata")
TRADE_DAY = datetime(2026, 10, 5, tzinfo=IST)  # Monday
OPEN = datetime(2026, 10, 5, 9, 15, tzinfo=IST).astimezone(timezone.utc)


def at(minutes_after_open: float) -> datetime:
    return OPEN + timedelta(minutes=minutes_after_open)


def ist(hour: int, minute: int, day: int = 5) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=IST).astimezone(timezone.utc)


def make_settings(tmp_path: Path | None = None, **signal_overrides):
    settings = load_settings(env={})
    if tmp_path is not None:
        settings = replace(settings, data_dir=tmp_path)
    if signal_overrides:
        settings = replace(settings, signals=replace(settings.signals, **signal_overrides))
    return settings


def make_engine(tmp_path: Path | None = None, *, store: Store | None = None, **signal_overrides) -> LiveEngine:
    settings = make_settings(tmp_path, **signal_overrides)
    store = store or Store(":memory:" if tmp_path is None else tmp_path / "test.sqlite3")
    return LiveEngine(settings, store)


class Bars:
    """Builds a full-session 1-minute series with a scripted post-event path."""

    def __init__(self, symbol: str, base: float = 1000.0, *, previous_close: float | None = None,
                 today_open: float | None = None, volume: float = 10_000.0, noise: float = 0.0004,
                 seed: int = 7) -> None:
        self.symbol = symbol
        self.base = base
        self.previous_close = previous_close if previous_close is not None else base
        self.today_open = today_open if today_open is not None else base
        self.volume = volume
        self.noise = noise
        self.rng = random.Random(seed)
        self.path: dict[int, float] = {}  # minute index -> per-bar return
        self.volume_mult: dict[int, float] = {}

    def move(self, start: int, end: int, per_bar: float, volume_mult: float = 1.0) -> "Bars":
        for minute in range(start, end):
            self.path[minute] = per_bar
            self.volume_mult[minute] = volume_mult
        return self

    def series(self, upto: datetime, *, minutes: int = 375, zero_volume_from: int | None = None) -> SymbolSeries:
        series = SymbolSeries(self.symbol, "1", self.previous_close, self.today_open)
        price = self.today_open
        rng = random.Random(self.symbol)
        for minute in range(minutes):
            start = OPEN + timedelta(minutes=minute)
            if start.timestamp() + 60 > upto.timestamp():
                break
            opened = price
            drift = self.path.get(minute, (rng.random() - 0.5) * 2 * self.noise)
            price = max(0.01, price * (1.0 + drift))
            high = max(opened, price) * (1 + self.noise)
            low = min(opened, price) * (1 - self.noise)
            volume = self.volume * self.volume_mult.get(minute, 1.0) * (0.8 + 0.4 * rng.random())
            if zero_volume_from is not None and minute >= zero_volume_from:
                volume = 0.0
            series.ts.append(start.timestamp())
            series.open.append(opened)
            series.high.append(high)
            series.low.append(low)
            series.close.append(price)
            series.volume.append(volume)
        return series


def background_universe(engine: LiveEngine, count: int = 120, exclude: set[str] | None = None) -> list[Bars]:
    exclude = exclude or set()
    bars = []
    for index, symbol in enumerate(engine.universe):
        if symbol in exclude:
            continue
        bars.append(Bars(symbol, 100 + index % 400, seed=index))
        if len(bars) >= count:
            break
    return bars


def snapshot(now: datetime, bars: list[Bars], *, status: str = "OK", session: str = "LIVE",
             benchmark: bool = True, sector_of: dict[str, str] | None = None,
             series_overrides: dict[str, SymbolSeries] | None = None, max_age: float = 150.0) -> MarketSnapshot:
    series = {item.symbol: item.series(now) for item in bars}
    if series_overrides:
        series.update(series_overrides)
    index = None
    if benchmark:
        index = IndexSeries("nifty")
        for minute in range(375):
            start = OPEN + timedelta(minutes=minute)
            if start.timestamp() + 60 > now.timestamp():
                break
            index.ts.append(start.timestamp())
            index.close.append(25000.0)
    return MarketSnapshot(
        fetched_at=now, as_of=now, psygrid_status=status, session_status=session, psygrid_clock=now,
        series=series, errors=[], shards_ok=22, shards_total=22, fetch_seconds=0.01, max_data_age_seconds=max_age,
        benchmark=index, vix=13.5, sector_of=sector_of or {},
    )


def nse_filing(symbol: str, company: str, subject: str, when: datetime, *, observed_delay: float = 30.0,
               summary: str | None = None) -> RawObservation:
    stamp = when.astimezone(IST).strftime("%d%m%Y%H%M%S")
    slug = "".join(word.capitalize() for word in subject.split())
    return RawObservation(
        provider_id="nse_announcements", source_tier=0, publisher="NSE Corporate Announcements",
        title=company, url=f"https://nsearchives.nseindia.com/corporate/{symbol}_{stamp}_{slug}.pdf",
        summary=summary or f"{company} has informed the Exchange about {subject}",
        published_at=when, observed_at=when + timedelta(seconds=observed_delay),
        raw={"nse_filing": {"symbol": symbol, "disseminated_at": when.isoformat(), "subject": subject}},
        source_quality="PRIMARY", category="corporate_disclosure",
    )


def media(title: str, when: datetime, *, publisher: str = "Economic Times", url: str | None = None,
          quality: str = "REPUTABLE_SECONDARY", summary: str = "", provider: str = "economictimes_default",
          observed_delay: float = 60.0) -> RawObservation:
    return RawObservation(
        provider_id=provider, source_tier=2 if quality == "REPUTABLE_SECONDARY" else 4, publisher=publisher,
        title=title, url=url or f"https://example.invalid/{abs(hash((title, publisher))) % 10**8}", summary=summary,
        published_at=when, observed_at=when + timedelta(seconds=observed_delay), raw={}, source_quality=quality,
        category="financial_media",
    )


def state_of(engine: LiveEngine, symbol: str) -> str | None:
    records = [record for record in engine.book.records.values() if record.symbol == symbol]
    if not records:
        return None
    return max(records, key=lambda record: record.last_state_change_at).state
