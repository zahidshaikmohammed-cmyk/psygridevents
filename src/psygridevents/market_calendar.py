"""IST session clock and trading-day calendar.

The calendar is deliberately conservative: weekends are never trading days,
configured exchange holidays (config/market_calendar.yaml, copied from the
official NSE holiday circular by the operator) are never trading days, and
on any other weekday the *live* PSYGRID feed is the final authority -- if no
fresh candles arrive after the open the engine reports
NO_LIVE_DATA instead of pretending the market is trading. Holidays are never
guessed.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from .settings import CONFIG_DIR, SessionSettings

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_CALENDAR_FILE = CONFIG_DIR / "market_calendar.yaml"


class SessionPhase(StrEnum):
    NON_TRADING_DAY = "NON_TRADING_DAY"
    OVERNIGHT = "OVERNIGHT"  # before pre-market on a trading day
    PRE_MARKET = "PRE_MARKET"
    MARKET = "MARKET"
    NEAR_CLOSE = "NEAR_CLOSE"  # market open, new aggressive entries suppressed
    POST_MARKET = "POST_MARKET"
    CLOSED = "CLOSED"  # after post-market end


TRADING_PHASES = {SessionPhase.MARKET, SessionPhase.NEAR_CLOSE}


@dataclass(frozen=True)
class MarketCalendar:
    session: SessionSettings
    holidays: frozenset[date]

    @classmethod
    def load(cls, session: SessionSettings, path: Path | None = None) -> "MarketCalendar":
        path = path or DEFAULT_CALENDAR_FILE
        holidays: set[date] = set()
        if path.exists():
            payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            for item in payload.get("holidays", []) or []:
                value = item.get("date") if isinstance(item, dict) else item
                if isinstance(value, date):
                    holidays.add(value)
                elif value:
                    holidays.add(date.fromisoformat(str(value)))
        return cls(session=session, holidays=frozenset(holidays))

    @staticmethod
    def ist(moment: datetime) -> datetime:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(IST)

    def trade_date(self, moment: datetime) -> date:
        return self.ist(moment).date()

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def phase(self, moment: datetime) -> SessionPhase:
        local = self.ist(moment)
        if not self.is_trading_day(local.date()):
            return SessionPhase.NON_TRADING_DAY
        now = local.time()
        s = self.session
        if now < s.pre_market_start:
            return SessionPhase.OVERNIGHT
        if now < s.market_open:
            return SessionPhase.PRE_MARKET
        if now < s.no_new_entries_after:
            return SessionPhase.MARKET
        if now < s.market_close:
            return SessionPhase.NEAR_CLOSE
        if now < s.post_market_end:
            return SessionPhase.POST_MARKET
        return SessionPhase.CLOSED

    def at(self, day: date, clock: time) -> datetime:
        return datetime.combine(day, clock, tzinfo=IST).astimezone(timezone.utc)

    def market_open_at(self, day: date) -> datetime:
        return self.at(day, self.session.market_open)

    def market_close_at(self, day: date) -> datetime:
        return self.at(day, self.session.market_close)

    def previous_trading_day(self, day: date) -> date:
        candidate = day - timedelta(days=1)
        for _ in range(15):
            if self.is_trading_day(candidate):
                return candidate
            candidate -= timedelta(days=1)
        return candidate

    def next_trading_day(self, day: date) -> date:
        candidate = day + timedelta(days=1)
        for _ in range(15):
            if self.is_trading_day(candidate):
                return candidate
            candidate += timedelta(days=1)
        return candidate

    def event_session_relation(self, public_at: datetime | None, as_of: datetime) -> str:
        """Where an event's public time sits relative to the session being traded at `as_of`.

        DURING_SESSION  -- became public while today's market was open.
        BEFORE_OPEN     -- became public after the previous close and before today's open
                           (reaction appears as the opening gap).
        AFTER_CLOSE     -- became public after today's close (no live reaction possible today).
        PRIOR_SESSION   -- became public during or before an earlier session (old news).
        UNKNOWN         -- no public timestamp.
        """
        if public_at is None:
            return "UNKNOWN"
        today = self.trade_date(as_of)
        if not self.is_trading_day(today):
            today = self.previous_trading_day(today)
        open_at = self.market_open_at(today)
        close_at = self.market_close_at(today)
        previous_close = self.market_close_at(self.previous_trading_day(today))
        if open_at <= public_at <= close_at:
            return "DURING_SESSION"
        if previous_close < public_at < open_at:
            return "BEFORE_OPEN"
        if public_at > close_at:
            return "AFTER_CLOSE"
        return "PRIOR_SESSION"
