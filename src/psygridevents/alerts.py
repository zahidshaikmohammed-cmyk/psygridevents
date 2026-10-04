"""Signal delivery: human-readable rendering, log channel, optional Telegram.

Delivery is downstream of signal generation and can never block or alter it:
every channel swallows and records its own failures.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

import httpx

from .settings import TelegramSettings

log = logging.getLogger("psygridevents.alerts")


def _pct(value: Any) -> str:
    try:
        return f"{float(value):+.2%}"
    except (TypeError, ValueError):
        return "n/a"


def format_signal(payload: dict[str, Any]) -> str:
    """Render one signal in the operator format (also used by Telegram and the log)."""
    rank = payload.get("rank")
    event = payload.get("event") or {}
    reaction = payload.get("market_reaction") or {}
    volume = payload.get("volume_confirmation") or {}
    strength = payload.get("relative_strength") or {}
    freshness = payload.get("freshness") or {}
    source = payload.get("source") or {}
    headline = event.get("headline") or ""
    if event.get("magnitude_crore"):
        headline = f"{headline} (₹{event['magnitude_crore']:,.0f} Cr)"
    ratio = volume.get("ratio")
    lines = [
        "PSYGRID EVENT SIGNAL",
        f"RANK #{rank}" if rank else "RANK: -",
        f"SYMBOL: {payload.get('symbol')}" + (f" ({payload['company']})" if payload.get("company") else ""),
        f"DIRECTION: {payload.get('direction')}",
        f"STATE: {payload.get('signal_state')}",
        f"OPPORTUNITY SCORE: {payload.get('opportunity_score')} (heuristic, not a probability)",
        "EVENT:",
        f"  {headline} [{event.get('subtype', '')}]",
        "EVENT AGE:",
        f"  {freshness.get('event_age')} ({freshness.get('freshness_status')})",
        "MARKET:",
        f"  {_pct(reaction.get('move_since_event'))} since event, price {payload.get('price')}, "
        f"intraday {_pct(payload.get('intraday_return'))}",
        "RELATIVE STRENGTH:",
        f"  {strength.get('label')} ({_pct(strength.get('value'))} vs {strength.get('basis')})",
        "VOLUME:",
        f"  {ratio:.1f}x baseline" if isinstance(ratio, (int, float)) else "  n/a",
        "VWAP:",
        f"  {str(payload.get('VWAP_state', 'unavailable')).capitalize()}",
        "EXHAUSTION:",
        f"  {str(payload.get('exhaustion_state', 'UNKNOWN')).capitalize()}",
        "WHY NOW:",
        f"  {payload.get('why_now')}",
        "INVALIDATION:",
        f"  {payload.get('invalidation')}",
        "SOURCE:",
        f"  {source.get('best_quality')} - {', '.join(source.get('publishers') or [])}",
    ]
    urls = payload.get("source_urls") or []
    if urls:
        lines.append(f"  {urls[0]}")
    flags = payload.get("risk_flags") or []
    if flags:
        lines.append("RISK FLAGS:")
        lines.append(f"  {', '.join(flags)}")
    return "\n".join(lines)


def format_transition(transition: Any, payload: dict[str, Any]) -> str:
    header = f"[{transition.from_state or 'NEW'} -> {transition.to_state}] {transition.symbol}"
    return header + "\n" + format_signal(payload)


class LogChannel:
    def __init__(self) -> None:
        self.sent = 0

    def deliver(self, transition: Any, payload: dict[str, Any]) -> None:
        self.sent += 1
        log.info("SIGNAL ALERT\n%s", format_transition(transition, payload))
        log.info("SIGNAL ALERT JSON %s", json.dumps(payload, default=str, ensure_ascii=False)[:20000])


class TelegramChannel:
    """Telegram Bot API sendMessage. Optional; configured only via TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID."""

    def __init__(self, settings: TelegramSettings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.sent = 0
        self.failures = 0
        self.last_error: str | None = None
        self._last_sent = 0.0
        self._client = httpx.AsyncClient(timeout=10.0, transport=transport) if settings.enabled else None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    async def deliver(self, transition: Any, payload: dict[str, Any]) -> None:
        if self._client is None:
            return
        wait = self.settings.min_interval_seconds - (time.monotonic() - self._last_sent)
        if wait > 0:
            await asyncio.sleep(min(wait, 30))
        text = format_transition(transition, payload)[:4000]
        try:
            response = await self._client.post(
                f"https://api.telegram.org/bot{self.settings.bot_token}/sendMessage",
                json={"chat_id": self.settings.chat_id, "text": text, "disable_web_page_preview": True},
            )
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            self.sent += 1
        except Exception as exc:  # noqa: BLE001 - delivery must never affect the engine
            self.failures += 1
            self.last_error = f"{type(exc).__name__}: {exc}"[:200]
            log.warning("telegram delivery failed: %s", self.last_error)
        finally:
            self._last_sent = time.monotonic()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def status(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "sent": self.sent, "failures": self.failures, "last_error": self.last_error}
