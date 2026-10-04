"""Runtime configuration: config/runtime.yaml defaults overridden by environment variables.

Every interval, threshold, URL and credential the production service uses is
configurable here. Credentials are read only from the environment (or an
EnvironmentFile loaded by systemd) and are never written to disk or logged.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, replace
from datetime import time
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
DEFAULT_RUNTIME_FILE = CONFIG_DIR / "runtime.yaml"


def _parse_time(value: Any, default: time) -> time:
    if isinstance(value, time):
        return value
    if value is None or value == "":
        return default
    text = str(value).strip()
    try:
        hour, minute = text.split(":")[:2]
        return time(int(hour), int(minute))
    except (ValueError, TypeError):
        raise ValueError(f"invalid HH:MM time value {value!r}") from None


def _env_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SessionSettings:
    """NSE equity session boundaries in IST (Asia/Kolkata)."""

    pre_market_start: time = time(8, 0)
    market_open: time = time(9, 15)
    no_new_entries_after: time = time(15, 0)
    market_close: time = time(15, 30)
    post_market_end: time = time(16, 30)


@dataclass(frozen=True)
class SignalSettings:
    top_n: int = 5
    min_opportunity_score: float = 60.0
    watch_min_score: float = 35.0
    rank_swap_margin: float = 4.0
    alert_cooldown_minutes: float = 15.0
    min_state_dwell_minutes: float = 2.0
    max_candidate_age_hours: float = 30.0
    max_event_age_for_entry_hours: float = 6.0
    second_order_discount: float = 0.75
    discovery_only_max_score: float = 55.0
    multiple_event_bonus: float = 3.0


@dataclass(frozen=True)
class MarketSettings:
    psygrid_base_url: str = "http://140.245.226.102:10000"
    psygrid_timeout_seconds: float = 10.0
    fetch_mode: str = "shards"  # "shards" (22 x /public/live-{a..v}.json) or "full" (/public/live.json)
    shard_letters: str = "abcdefghijklmnopqrstuv"
    max_data_age_seconds: float = 150.0
    fetch_index_context: bool = True
    fetch_sector_context: bool = True
    benchmark_route: str = "nifty"
    vix_route: str = "indiavix"
    loop_interval_seconds: float = 60.0
    loop_offset_seconds: float = 6.0
    min_bars_for_baseline: int = 10


@dataclass(frozen=True)
class TelegramSettings:
    bot_token: str | None = None
    chat_id: str | None = None
    min_interval_seconds: float = 20.0

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass(frozen=True)
class AISettings:
    enabled: bool = False
    api_key: str | None = None
    model: str = "claude-opus-5-5"
    max_calls_per_day: int = 200
    min_materiality_score: float = 0.6
    timeout_seconds: float = 20.0

    @property
    def available(self) -> bool:
        return bool(self.enabled and self.api_key)


@dataclass(frozen=True)
class Settings:
    data_dir: Path = ROOT / "data"
    db_filename: str = "psygridevents.sqlite3"
    sources_file: Path = CONFIG_DIR / "sources.yaml"
    instruments_file: Path = CONFIG_DIR / "instruments.json"
    api_host: str = "127.0.0.1"
    api_port: int = 10100
    log_level: str = "INFO"
    log_json: bool = True
    http_user_agent: str = "PSYGRIDEVENTS/1.0 (+event-intelligence; contact: operator)"
    source_default_poll_seconds: float = 120.0
    source_off_hours_poll_multiplier: float = 5.0
    source_timeout_seconds: float = 15.0
    source_max_bytes: int = 5_000_000
    observation_retention_days: int = 45
    story_window_hours: float = 72.0
    recent_event_memory_hours: float = 168.0
    session: SessionSettings = field(default_factory=SessionSettings)
    signals: SignalSettings = field(default_factory=SignalSettings)
    market: MarketSettings = field(default_factory=MarketSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    ai: AISettings = field(default_factory=AISettings)

    @property
    def db_path(self) -> Path:
        return self.data_dir / self.db_filename

    def safe_dict(self) -> dict[str, Any]:
        """Settings for /system/status with every credential redacted."""

        def convert(value: Any) -> Any:
            if hasattr(value, "__dataclass_fields__"):
                result = {}
                for item in fields(value):
                    raw = getattr(value, item.name)
                    if item.name in {"bot_token", "api_key"}:
                        result[item.name] = "***" if raw else None
                    elif item.name == "chat_id":
                        result[item.name] = "***" if raw else None
                    else:
                        result[item.name] = convert(raw)
                return result
            if isinstance(value, (Path, time)):
                return str(value)
            return value

        return convert(self)


def _section(cls: type, payload: dict[str, Any] | None, defaults: Any) -> Any:
    if not payload:
        return defaults
    known = {item.name for item in fields(cls)}
    values = {}
    for key, value in payload.items():
        if key not in known:
            raise ValueError(f"unknown {cls.__name__} setting {key!r} in runtime config")
        default_value = getattr(defaults, key)
        if isinstance(default_value, time):
            values[key] = _parse_time(value, default_value)
        else:
            values[key] = value
    return replace(defaults, **values)


def load_settings(path: Path | None = None, env: dict[str, str] | None = None) -> Settings:
    """Load runtime.yaml (if present) then apply environment overrides."""
    env = dict(os.environ if env is None else env)
    path = path or Path(env.get("PSYGRIDEVENTS_RUNTIME_CONFIG", DEFAULT_RUNTIME_FILE))
    payload: dict[str, Any] = {}
    if path and Path(path).exists():
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    base = Settings()
    top_level = {key: value for key, value in payload.items() if key not in {"session", "signals", "market", "telegram", "ai"}}
    known_top = {item.name for item in fields(Settings)}
    for key in top_level:
        if key not in known_top:
            raise ValueError(f"unknown runtime setting {key!r}")
    converted: dict[str, Any] = {}
    for key, value in top_level.items():
        if key in {"data_dir", "sources_file", "instruments_file"}:
            candidate = Path(value)
            converted[key] = candidate if candidate.is_absolute() else ROOT / candidate
        else:
            converted[key] = value
    settings = replace(
        base,
        **converted,
        session=_section(SessionSettings, payload.get("session"), base.session),
        signals=_section(SignalSettings, payload.get("signals"), base.signals),
        market=_section(MarketSettings, payload.get("market"), base.market),
        telegram=_section(TelegramSettings, payload.get("telegram"), base.telegram),
        ai=_section(AISettings, payload.get("ai"), base.ai),
    )

    # Environment overrides (credentials ONLY come from here).
    if env.get("PSYGRIDEVENTS_DATA_DIR"):
        settings = replace(settings, data_dir=Path(env["PSYGRIDEVENTS_DATA_DIR"]))
    if env.get("PSYGRIDEVENTS_API_HOST"):
        settings = replace(settings, api_host=env["PSYGRIDEVENTS_API_HOST"])
    if env.get("PSYGRIDEVENTS_API_PORT"):
        settings = replace(settings, api_port=int(env["PSYGRIDEVENTS_API_PORT"]))
    if env.get("PSYGRIDEVENTS_LOG_LEVEL"):
        settings = replace(settings, log_level=env["PSYGRIDEVENTS_LOG_LEVEL"].upper())
    if env.get("PSYGRIDEVENTS_LOG_JSON"):
        settings = replace(settings, log_json=_env_bool(env["PSYGRIDEVENTS_LOG_JSON"]))
    if env.get("PSYGRIDEVENTS_SOURCES_FILE"):
        settings = replace(settings, sources_file=Path(env["PSYGRIDEVENTS_SOURCES_FILE"]))
    if env.get("PSYGRIDEVENTS_USER_AGENT"):
        settings = replace(settings, http_user_agent=env["PSYGRIDEVENTS_USER_AGENT"])

    market = settings.market
    if env.get("PSYGRID_BASE_URL"):
        market = replace(market, psygrid_base_url=env["PSYGRID_BASE_URL"].rstrip("/"))
    if env.get("PSYGRID_FETCH_MODE"):
        mode = env["PSYGRID_FETCH_MODE"].strip().lower()
        if mode not in {"shards", "full"}:
            raise ValueError("PSYGRID_FETCH_MODE must be 'shards' or 'full'")
        market = replace(market, fetch_mode=mode)
    if env.get("PSYGRID_MAX_DATA_AGE_SECONDS"):
        market = replace(market, max_data_age_seconds=float(env["PSYGRID_MAX_DATA_AGE_SECONDS"]))
    settings = replace(settings, market=market)

    signals = settings.signals
    if env.get("PSYGRIDEVENTS_MIN_OPPORTUNITY_SCORE"):
        signals = replace(signals, min_opportunity_score=float(env["PSYGRIDEVENTS_MIN_OPPORTUNITY_SCORE"]))
    if env.get("PSYGRIDEVENTS_TOP_N"):
        signals = replace(signals, top_n=int(env["PSYGRIDEVENTS_TOP_N"]))
    settings = replace(settings, signals=signals)

    telegram = replace(
        settings.telegram,
        bot_token=env.get("TELEGRAM_BOT_TOKEN") or None,
        chat_id=env.get("TELEGRAM_CHAT_ID") or None,
    )
    ai = settings.ai
    if env.get("PSYGRIDEVENTS_AI_ENABLED"):
        ai = replace(ai, enabled=_env_bool(env["PSYGRIDEVENTS_AI_ENABLED"]))
    if env.get("PSYGRIDEVENTS_AI_MODEL"):
        ai = replace(ai, model=env["PSYGRIDEVENTS_AI_MODEL"])
    if env.get("PSYGRIDEVENTS_AI_MAX_CALLS_PER_DAY"):
        ai = replace(ai, max_calls_per_day=int(env["PSYGRIDEVENTS_AI_MAX_CALLS_PER_DAY"]))
    ai = replace(ai, api_key=env.get("ANTHROPIC_API_KEY") or None)
    return replace(settings, telegram=telegram, ai=ai)
