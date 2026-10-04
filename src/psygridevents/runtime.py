"""Production service runtime (asyncio) for Oracle Always Free.

Loops:
  * sources  -- probes and polls every configured public source on its own
                schedule (faster pre-market/in-market, slower off-hours), with
                per-provider backoff. One failing provider never stops the rest.
  * market   -- once per minute (aligned to the minute + offset) reads the whole
                PSYGRID universe in bulk and runs the evaluation/ranking cycle.
  * daily    -- pre-market preparation (re-probe sources, refresh issuer master,
                verify PSYGRID) and post-market close-out (outcomes, volume
                profiles, diagnostics, pruning).
  * ai       -- optional, budget-capped assist for ambiguous material events.

Everything durable lives in SQLite; on restart `LiveEngine.restore()` reloads
stories, events, signal states and open outcomes, so nothing is re-announced
and no history is lost. SIGTERM/SIGINT trigger a graceful shutdown.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from . import __version__
from .ai_assist import AIAssist
from .alerts import LogChannel, TelegramChannel
from .api import ApiServer, Published
from .issuer_refresh import refresh_issuers
from .live_engine import LiveEngine
from .market_calendar import TRADING_PHASES, SessionPhase
from .market_snapshot import MarketSnapshot, PsygridBulkClient
from .providers import SourceHttpClient, SourceRegistry, load_source_specs
from .settings import Settings, load_settings
from .storage import Store
from .universe import load_instruments

log = logging.getLogger("psygridevents.runtime")

ACTIVE_SOURCE_PHASES = {SessionPhase.PRE_MARKET, SessionPhase.MARKET, SessionPhase.NEAR_CLOSE, SessionPhase.POST_MARKET}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if settings.log_json else logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, settings.log_level, logging.INFO))
    logging.getLogger("httpx").setLevel(logging.WARNING)


class ServiceRuntime:
    def __init__(self, settings: Settings, *, store: Store | None = None,
                 http: SourceHttpClient | None = None, market: PsygridBulkClient | None = None) -> None:
        self.settings = settings
        self.started_at = datetime.now(timezone.utc)
        self.store = store or Store(settings.db_path)
        self.universe = load_instruments(settings.instruments_file)
        self.engine = LiveEngine(settings, self.store, universe=self.universe)
        self.registry = SourceRegistry(load_source_specs(settings.sources_file),
                                       persisted=self.store.load_provider_health())
        self.http = http or SourceHttpClient(
            user_agent=settings.http_user_agent, timeout_seconds=settings.source_timeout_seconds,
            max_bytes=settings.source_max_bytes,
        )
        self.market = market or PsygridBulkClient(
            settings.market.psygrid_base_url, timeout=settings.market.psygrid_timeout_seconds,
            fetch_mode=settings.market.fetch_mode, shard_letters=settings.market.shard_letters,
            max_data_age_seconds=settings.market.max_data_age_seconds,
            benchmark_route=settings.market.benchmark_route if settings.market.fetch_index_context else None,
            vix_route=settings.market.vix_route if settings.market.fetch_index_context else None,
            fetch_sectors=settings.market.fetch_sector_context, fallback_sector_of=self.engine.graph.sector_of,
        )
        self.ai = AIAssist(settings.ai)
        self.log_channel = LogChannel()
        self.telegram = TelegramChannel(settings.telegram)
        self.published = Published()
        self.stop_event = asyncio.Event()
        self.last_snapshot: MarketSnapshot | None = None
        self.last_market_cycle: datetime | None = None
        self.last_source_cycle: datetime | None = None
        self.last_cycle_error: str | None = None
        self.cycle_seconds: float | None = None
        self.market_errors = 0
        self.api: ApiServer | None = None
        self._tasks: list[asyncio.Task] = []
        self._source_semaphore = asyncio.Semaphore(6)

    # ------------------------------------------------------------------ status
    def phase(self, now: datetime | None = None) -> SessionPhase:
        return self.engine.calendar.phase(now or datetime.now(timezone.utc))

    def liveness(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        loop_age = (now - self.last_market_cycle).total_seconds() if self.last_market_cycle else None
        status = "OK"
        reasons: list[str] = []
        if loop_age is None:
            if (now - self.started_at).total_seconds() > 240:
                status = "ERROR"
                reasons.append("no market cycle has completed")
            else:
                status = "STARTING"
                reasons.append("first cycle pending")
        elif loop_age > 400:
            status = "ERROR"
            reasons.append(f"market loop stalled ({loop_age:.0f}s)")
        market_health = self.last_snapshot.health() if self.last_snapshot else "DISCONNECTED"
        if status == "OK" and self.phase(now) in TRADING_PHASES and market_health not in ("HEALTHY",):
            status = "DEGRADED"
            reasons.append(f"market data {market_health}")
        healthy_sources = sum(1 for item in self.registry.statuses() if item.state in ("HEALTHY", "STALE", "DEGRADED"))
        if status == "OK" and healthy_sources == 0 and (now - self.started_at).total_seconds() > 300:
            status = "DEGRADED"
            reasons.append("no news source is healthy")
        return {"status": status, "reasons": reasons, "market_loop_age_seconds": loop_age,
                "market_health": market_health, "healthy_sources": healthy_sources, "version": __version__,
                "uptime_seconds": round((now - self.started_at).total_seconds(), 1)}

    def publish(self) -> None:
        now = datetime.now(timezone.utc)
        snapshot = self.last_snapshot
        signals = [record.payload for record in self.engine.book.active() if record.state != "NO_SIGNAL"]
        signals.sort(key=lambda item: -float(item.get("opportunity_score") or 0))
        market: dict[str, Any] = {"health": "DISCONNECTED", "psygrid_base_url": self.settings.market.psygrid_base_url}
        if snapshot is not None:
            market = {
                "health": snapshot.health(), "psygrid_status": snapshot.psygrid_status,
                "session_status": snapshot.session_status,
                "psygrid_clock": snapshot.psygrid_clock.isoformat() if snapshot.psygrid_clock else None,
                "fetched_at": snapshot.fetched_at.isoformat(), "fetch_seconds": snapshot.fetch_seconds,
                "shards_ok": snapshot.shards_ok, "shards_total": snapshot.shards_total,
                "latest_bar_age_seconds": snapshot.data_age_seconds(), "coverage": snapshot.coverage(),
                "regime": snapshot.regime(), "errors": snapshot.errors[:20],
                "benchmark": snapshot.benchmark.route if snapshot.benchmark else None,
                "psygrid_base_url": self.settings.market.psygrid_base_url,
                "fetch_mode": self.settings.market.fetch_mode, "consecutive_errors": self.market_errors,
            }
        evaluation = self.engine.last_evaluation
        counters = dict(self.engine.counters)
        metrics = {
            **{f"counter_{key}": value for key, value in counters.items()},
            "events_in_memory": len(self.engine.events), "stories_in_memory": len(self.engine.storybook.stories),
            "active_signals": len(self.engine.book.active()), "top_count": len(self.engine.top),
            "market_cycle_seconds": self.cycle_seconds or 0.0, "market_errors_consecutive": self.market_errors,
            "providers_healthy": sum(1 for item in self.registry.statuses() if item.state == "HEALTHY"),
            "providers_total": len(self.registry.statuses()), "alerts_logged": self.log_channel.sent,
            "telegram_sent": self.telegram.sent, "telegram_failures": self.telegram.failures,
            "memory_rss_mb": _rss_mb(),
        }
        if evaluation is not None:
            metrics["candidates_last_cycle"] = evaluation.candidates
            for state, value in evaluation.counts.items():
                metrics[f"signals_{state.lower()}"] = value
        self.published.set({
            "generated_at": now.isoformat(),
            "top": [entry.payload for entry in self.engine.top],
            "signals": signals[:500],
            "providers": [item.to_dict() for item in self.registry.statuses()],
            "market": market,
            "system": {
                "version": __version__, "phase": self.phase(now).value,
                "trade_date": self.engine.calendar.trade_date(now).isoformat(),
                "started_at": self.started_at.isoformat(), "market_health": market.get("health"),
                "min_opportunity_score": self.settings.signals.min_opportunity_score,
                "top_n": self.settings.signals.top_n, "universe_size": len(self.universe),
                "issuer_names_loaded": len(self.engine.issuer_names), "last_cycle_error": self.last_cycle_error,
                "last_market_cycle": self.last_market_cycle.isoformat() if self.last_market_cycle else None,
                "last_source_cycle": self.last_source_cycle.isoformat() if self.last_source_cycle else None,
                "ai": self.ai.status(), "telegram": self.telegram.status(), "settings": self.settings.safe_dict(),
            },
            "metrics": metrics,
        })

    # ------------------------------------------------------------------ loops
    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=max(0.0, seconds))
        except asyncio.TimeoutError:
            pass

    def _interval(self, item: Any, phase: SessionPhase) -> float:
        if phase in ACTIVE_SOURCE_PHASES:
            return item.spec.poll_seconds
        return item.spec.off_hours_poll_seconds or item.spec.poll_seconds * self.settings.source_off_hours_poll_multiplier

    async def _run_source(self, item: Any, now: datetime, phase: SessionPhase) -> list:
        async with self._source_semaphore:
            if self.registry.needs_probe(item, now):
                ok = await self.registry.probe(item, self.http, now)
                if not ok:
                    return []
            if not item.tracker.status.active or not item.tracker.is_due(now):
                return []
            result = await self.registry.poll(item, self.http, since=None, now=now, interval_seconds=self._interval(item, phase))
            self.store.save_provider_health(item.spec.id, item.tracker.status.to_dict())
            return result.observations if result else []

    async def source_loop(self) -> None:
        while not self.stop_event.is_set():
            now = datetime.now(timezone.utc)
            phase = self.phase(now)
            try:
                due = [
                    item for item in self.registry.sources.values()
                    if self.registry.needs_probe(item, now) or (item.tracker.status.active and item.tracker.is_due(now))
                ]
                if due:
                    results = await asyncio.gather(*(self._run_source(item, now, phase) for item in due), return_exceptions=True)
                    observations = []
                    for result in results:
                        if isinstance(result, Exception):
                            log.warning("source task error: %s", result)
                        else:
                            observations.extend(result)
                    if observations:
                        events = self.engine.ingest(observations, datetime.now(timezone.utc))
                        log.info("ingested %d observations -> %d events updated", len(observations), len(events))
                    for item in self.registry.sources.values():
                        item.tracker.refresh_staleness(now)
                    self.last_source_cycle = now
                    self.publish()
            except Exception:  # noqa: BLE001 - the loop must survive anything
                log.exception("source loop iteration failed")
            await self._sleep(5.0)

    async def market_cycle(self) -> None:
        started = time.monotonic()
        now = datetime.now(timezone.utc)
        phase = self.phase(now)
        snapshot = None
        try:
            snapshot = await self.market.snapshot(as_of=now, universe=self.universe)
            self.market_errors = 0 if snapshot.shards_ok else self.market_errors + 1
        except Exception as exc:  # noqa: BLE001
            self.market_errors += 1
            log.warning("PSYGRID snapshot failed: %s", exc)
        self.last_snapshot = snapshot
        result = self.engine.evaluate(snapshot, now)
        for transition in result.alerts:
            record = self.engine.book.get(transition.signal_key)
            payload = record.payload if record else {}
            ranked = next((entry for entry in self.engine.top if entry.signal_key == transition.signal_key), None)
            if ranked:
                payload = ranked.payload
            self.log_channel.deliver(transition, payload)
            if self.telegram.enabled:
                asyncio.create_task(self.telegram.deliver(transition, payload))
        self.cycle_seconds = round(time.monotonic() - started, 3)
        self.last_market_cycle = now
        self.last_cycle_error = None
        if result.transitions:
            log.info("cycle phase=%s health=%s candidates=%d transitions=%d top=%s (%.2fs)", phase.value,
                     result.market_health, result.candidates, len(result.transitions),
                     [entry.symbol for entry in result.top], self.cycle_seconds)
        self.publish()

    async def market_loop(self) -> None:
        interval = self.settings.market.loop_interval_seconds
        offset = self.settings.market.loop_offset_seconds
        while not self.stop_event.is_set():
            phase = self.phase()
            try:
                await self.market_cycle()
            except Exception as exc:  # noqa: BLE001
                self.last_cycle_error = f"{type(exc).__name__}: {exc}"[:300]
                log.exception("market cycle failed")
            period = interval if phase in ACTIVE_SOURCE_PHASES else max(interval, 300.0)
            now = time.time()
            next_tick = (now // period + 1) * period + offset
            await self._sleep(next_tick - now)

    async def daily_loop(self) -> None:
        while not self.stop_event.is_set():
            now = datetime.now(timezone.utc)
            phase = self.phase(now)
            day = self.engine.calendar.trade_date(now).isoformat()
            try:
                if phase == SessionPhase.PRE_MARKET and self.store.get_kv("premarket_done") != day:
                    await self.pre_market(now)
                    self.store.set_kv("premarket_done", day)
                if phase in (SessionPhase.POST_MARKET, SessionPhase.CLOSED) and self.store.get_kv("postmarket_done") != day \
                        and self.engine.calendar.is_trading_day(self.engine.calendar.trade_date(now)):
                    await self.post_market(now)
                    self.store.set_kv("postmarket_done", day)
            except Exception:  # noqa: BLE001
                log.exception("daily routine failed")
            await self._sleep(60.0)

    async def pre_market(self, now: datetime) -> None:
        log.info("pre-market preparation for %s", self.engine.calendar.trade_date(now))
        for item in self.registry.sources.values():
            status = item.tracker.status
            if status.last_probe_ok is False:
                status.next_due_at = now  # re-probe failed sources before the open
        await self.refresh_issuers_if_stale(now, max_age_hours=20)
        healthy, error = await self.market.health()
        log.info("PSYGRID health: %s %s", "OK" if healthy else "NOT OK", error or "")
        self.engine.load_volume_history(self.engine.calendar.trade_date(now).isoformat())

    async def post_market(self, now: datetime) -> None:
        log.info("post-market close-out")
        snapshot = self.last_snapshot
        diagnostics = self.engine.end_of_day(snapshot, now)
        pruned = self.store.prune_observations(now - timedelta(days=self.settings.observation_retention_days))
        log.info("daily diagnostics saved: %s (pruned %d observation payloads)",
                 {key: diagnostics[key] for key in ("events", "actionable_signals", "outcomes_recorded")}, pruned)

    async def refresh_issuers_if_stale(self, now: datetime, *, max_age_hours: float) -> None:
        last = self.store.get_kv("issuers_refreshed_at")
        if last and now - datetime.fromisoformat(last) < timedelta(hours=max_age_hours):
            return
        report = await refresh_issuers(self.store, self.http, frozenset(self.universe))
        log.info("issuer master refresh: %s", report)
        if any(item.get("ok") for item in report.values()):
            self.store.set_kv("issuers_refreshed_at", now.isoformat())
            self.engine.reload_issuers()

    async def ai_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.ai.available and self.engine.ai_queue:
                event_id = self.engine.ai_queue.popleft()
                event = self.engine.events.get(event_id)
                if event is not None and event.ai_assist is None:
                    assist = await self.ai.classify(
                        headline=event.headline, summary=event.summary, symbols=list(event.symbols),
                        today=self.engine.calendar.trade_date(datetime.now(timezone.utc)),
                    )
                    if assist:
                        self.engine.apply_ai_assist(event_id, assist)
                continue
            await self._sleep(5.0)

    # ------------------------------------------------------------------ lifecycle
    async def run(self, *, with_api: bool = True) -> None:
        now = datetime.now(timezone.utc)
        if not self.store.integrity_ok():
            raise RuntimeError(f"SQLite integrity check failed for {self.store.path}; restore from backup")
        restored = self.engine.restore(now)
        log.info("PSYGRIDEVENTS %s starting: universe=%d sources=%d restored=%s psygrid=%s db=%s", __version__,
                 len(self.universe), len(self.registry.sources), restored, self.settings.market.psygrid_base_url,
                 self.store.path)
        if with_api:
            self.api = ApiServer(self.settings.api_host, self.settings.api_port, self.published, self.store, self.liveness,
                                 token=self.settings.api_token)
            self.api.start()
        self.publish()
        try:
            await self.refresh_issuers_if_stale(now, max_age_hours=24 * 7)
        except Exception:  # noqa: BLE001
            log.exception("initial issuer refresh failed (curated aliases remain in use)")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, self.stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
        self._tasks = [
            asyncio.create_task(self.source_loop(), name="sources"),
            asyncio.create_task(self.market_loop(), name="market"),
            asyncio.create_task(self.daily_loop(), name="daily"),
            asyncio.create_task(self.ai_loop(), name="ai"),
        ]
        await self.stop_event.wait()
        await self.shutdown()

    async def shutdown(self) -> None:
        log.info("shutting down gracefully")
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for item in self.registry.sources.values():
            self.store.save_provider_health(item.spec.id, item.tracker.status.to_dict())
        self.store.set_kv("last_clean_shutdown", datetime.now(timezone.utc).isoformat())
        if self.api is not None:
            self.api.stop()
        await self.http.aclose()
        await self.market.aclose()
        await self.telegram.aclose()
        self.store.close()
        log.info("shutdown complete")


def _rss_mb() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024.0, 1)
    except OSError:
        pass
    return 0.0


async def probe_sources(settings: Settings) -> list[dict[str, Any]]:
    """One-shot probe of every source (used by `psygridevents --probe-sources` on the server)."""
    registry = SourceRegistry(load_source_specs(settings.sources_file))
    http = SourceHttpClient(user_agent=settings.http_user_agent, timeout_seconds=settings.source_timeout_seconds,
                            max_bytes=settings.source_max_bytes)
    now = datetime.now(timezone.utc)
    try:
        await asyncio.gather(*(registry.probe(item, http, now) for item in registry.sources.values()
                               if registry.needs_probe(item, now)))
    finally:
        await http.aclose()
    return [item.to_dict() for item in registry.statuses()]


def main() -> None:
    settings = load_settings()
    configure_logging(settings)
    os.makedirs(settings.data_dir, exist_ok=True)
    runtime = ServiceRuntime(settings)
    asyncio.run(runtime.run())


if __name__ == "__main__":
    main()
