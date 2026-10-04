"""Durable structured persistence (SQLite, WAL) for the production service.

This is the primary historical database. It survives process restarts and
holds everything the engine must not forget: raw evidence observations,
canonical stories/events, exposures, the stateful signal book and its
transition log, outcome measurements, provider health, issuer reference data,
per-day volume profiles and daily diagnostics.

SQLite in WAL mode is deliberately chosen for Oracle Always Free: no extra
service, no network hop, crash-safe commits, a few MB of RAM, and a single
file that is trivial to back up (`sqlite3 db ".backup file"`).

`.psygridevents_state.json` (state_store.PublicationStateStore) remains only
for the legacy `--once/--watch` CLI modes; the service never depends on it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    obs_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL,
    source_quality TEXT NOT NULL,
    source_tier INTEGER NOT NULL,
    publisher TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    summary TEXT,
    published_at TEXT,
    observed_at TEXT NOT NULL,
    ingestion_latency_seconds REAL,
    content_key TEXT NOT NULL,
    story_id TEXT,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_content_key ON observations(content_key);
CREATE INDEX IF NOT EXISTS idx_obs_observed_at ON observations(observed_at);
CREATE INDEX IF NOT EXISTS idx_obs_story ON observations(story_id);

CREATE TABLE IF NOT EXISTS stories (
    story_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    latest_seen TEXT NOT NULL,
    first_public_at TEXT,
    source_count INTEGER NOT NULL,
    publisher_count INTEGER NOT NULL,
    best_quality TEXT NOT NULL,
    confirmation_status TEXT NOT NULL,
    symbols_json TEXT NOT NULL,
    event_type TEXT,
    contradictions_json TEXT NOT NULL DEFAULT '[]',
    payload_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stories_latest ON stories(latest_seen);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    story_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    subtype TEXT,
    headline TEXT NOT NULL,
    symbols_json TEXT NOT NULL,
    direction TEXT NOT NULL,
    direction_confidence REAL NOT NULL,
    direction_basis TEXT,
    materiality TEXT NOT NULL,
    materiality_score REAL NOT NULL,
    novelty_status TEXT NOT NULL,
    novelty_score REAL NOT NULL,
    modality TEXT NOT NULL,
    negated INTEGER NOT NULL,
    source_quality TEXT NOT NULL,
    confirmation_status TEXT NOT NULL,
    public_at TEXT,
    first_seen TEXT NOT NULL,
    session_phase_at_event TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_public ON events(public_at);
CREATE INDEX IF NOT EXISTS idx_events_first_seen ON events(first_seen);

CREATE TABLE IF NOT EXISTS exposures (
    event_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    relationship TEXT NOT NULL,
    mechanism TEXT NOT NULL,
    expected_direction TEXT NOT NULL,
    confidence REAL NOT NULL,
    materiality REAL NOT NULL,
    hop INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (event_id, symbol, relationship)
);
CREATE INDEX IF NOT EXISTS idx_exposures_symbol ON exposures(symbol);

CREATE TABLE IF NOT EXISTS signals (
    signal_key TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    event_id TEXT NOT NULL,
    story_id TEXT,
    direction TEXT NOT NULL,
    state TEXT NOT NULL,
    opportunity_score REAL NOT NULL,
    first_seen_at TEXT NOT NULL,
    first_actionable_at TEXT,
    last_state_change_at TEXT NOT NULL,
    last_alert_at TEXT,
    last_alert_state TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    trade_date TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_signals_active ON signals(active, trade_date);

CREATE TABLE IF NOT EXISTS signal_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    at TEXT NOT NULL,
    opportunity_score REAL,
    reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_transitions_key ON signal_transitions(signal_key);
CREATE INDEX IF NOT EXISTS idx_transitions_at ON signal_transitions(at);

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id TEXT PRIMARY KEY,
    signal_key TEXT NOT NULL,
    event_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    event_type TEXT,
    direction TEXT NOT NULL,
    reference_kind TEXT NOT NULL,
    reference_at TEXT NOT NULL,
    reference_price REAL NOT NULL,
    state_at_reference TEXT NOT NULL,
    opportunity_score_at_reference REAL,
    actionable INTEGER NOT NULL,
    market_state_json TEXT NOT NULL,
    returns_json TEXT NOT NULL DEFAULT '{}',
    mfe REAL,
    mae REAL,
    time_to_peak_minutes REAL,
    time_to_invalidation_minutes REAL,
    became_exhausted INTEGER NOT NULL DEFAULT 0,
    final_state TEXT,
    completed INTEGER NOT NULL DEFAULT 0,
    trade_date TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcomes_open ON outcomes(completed, trade_date);

CREATE TABLE IF NOT EXISTS provider_health (
    provider_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS issuers (
    symbol TEXT PRIMARY KEY,
    company_name TEXT,
    isin TEXT,
    industry TEXT,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS volume_profiles (
    symbol TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    buckets_json TEXT NOT NULL,
    total_volume REAL NOT NULL,
    bars INTEGER NOT NULL,
    PRIMARY KEY (symbol, trade_date)
);

CREATE TABLE IF NOT EXISTS daily_diagnostics (
    trade_date TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_default)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return iso(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if hasattr(value, "__dataclass_fields__"):
        return {name: getattr(value, name) for name in value.__dataclass_fields__}
    return str(value)


@dataclass(frozen=True)
class StoredSignal:
    signal_key: str
    symbol: str
    event_id: str
    story_id: str | None
    direction: str
    state: str
    opportunity_score: float
    first_seen_at: datetime
    first_actionable_at: datetime | None
    last_state_change_at: datetime
    last_alert_at: datetime | None
    last_alert_state: str | None
    active: bool
    trade_date: str
    payload: dict[str, Any]


class Store:
    """Thread-safe SQLite store. One connection, serialized by an RLock.

    The API server threads and the asyncio engine share this object. Every
    write is committed immediately (WAL + synchronous=NORMAL), so a crash or
    SIGKILL loses at most the in-flight statement, never the database.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._migrate()

    # ------------------------------------------------------------------ infra
    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema version {version} is newer than this code ({SCHEMA_VERSION}); refusing to run"
            )
        self._conn.executescript(_SCHEMA)
        self._conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def _execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)).fetchall())

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    def integrity_ok(self) -> bool:
        rows = self._query("PRAGMA quick_check")
        return bool(rows) and rows[0][0] == "ok"

    # --------------------------------------------------------------------- kv
    def set_kv(self, key: str, value: Any) -> None:
        self._execute(
            "INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, dumps(value), iso(datetime.now(timezone.utc))),
        )

    def get_kv(self, key: str, default: Any = None) -> Any:
        rows = self._query("SELECT value FROM kv WHERE key=?", (key,))
        if not rows:
            return default
        try:
            return json.loads(rows[0]["value"])
        except json.JSONDecodeError:
            return default

    # ----------------------------------------------------------- observations
    def has_content_key(self, content_key: str) -> bool:
        return bool(self._query("SELECT 1 FROM observations WHERE content_key=? LIMIT 1", (content_key,)))

    def known_content_keys(self, since: datetime) -> set[str]:
        rows = self._query("SELECT content_key FROM observations WHERE observed_at >= ?", (iso(since),))
        return {row["content_key"] for row in rows}

    def insert_observation(self, row: dict[str, Any]) -> bool:
        cursor = self._execute(
            "INSERT OR IGNORE INTO observations(obs_id, provider_id, source_quality, source_tier, publisher, title, url, "
            "summary, published_at, observed_at, ingestion_latency_seconds, content_key, story_id, raw_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                row["obs_id"], row["provider_id"], row["source_quality"], row["source_tier"], row["publisher"],
                row["title"], row["url"], row.get("summary"), iso(row.get("published_at")), iso(row["observed_at"]),
                row.get("ingestion_latency_seconds"), row["content_key"], row.get("story_id"),
                dumps(row.get("raw", {}))[:8000],
            ),
        )
        return cursor.rowcount > 0

    def set_observation_story(self, obs_id: str, story_id: str) -> None:
        self._execute("UPDATE observations SET story_id=? WHERE obs_id=?", (story_id, obs_id))

    def observations_for_story(self, story_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT obs_id, provider_id, source_quality, source_tier, publisher, title, url, summary, published_at, "
            "observed_at, ingestion_latency_seconds, raw_json FROM observations WHERE story_id=? ORDER BY observed_at",
            (story_id,),
        )
        result = []
        for row in rows:
            item = dict(row)
            try:
                item["raw"] = json.loads(item.pop("raw_json") or "{}")
            except json.JSONDecodeError:
                item["raw"] = {}
            result.append(item)
        return result

    def recent_observations(self, since: datetime, limit: int = 2000) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM observations WHERE observed_at >= ? ORDER BY observed_at DESC LIMIT ?",
            (iso(since), limit),
        )
        return [dict(row) for row in rows]

    def prune_observations(self, older_than: datetime) -> int:
        """Drop bulky raw payloads/summaries of old observations (evidence titles/urls are kept)."""
        cursor = self._execute(
            "UPDATE observations SET raw_json=NULL, summary=NULL WHERE observed_at < ? AND raw_json IS NOT NULL",
            (iso(older_than),),
        )
        return cursor.rowcount

    # ---------------------------------------------------------------- stories
    def upsert_story(self, story: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO stories(story_id, title, first_seen, latest_seen, first_public_at, source_count, publisher_count, "
            "best_quality, confirmation_status, symbols_json, event_type, contradictions_json, payload_json, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(story_id) DO UPDATE SET title=excluded.title, "
            "latest_seen=excluded.latest_seen, first_public_at=excluded.first_public_at, source_count=excluded.source_count, "
            "publisher_count=excluded.publisher_count, best_quality=excluded.best_quality, "
            "confirmation_status=excluded.confirmation_status, symbols_json=excluded.symbols_json, "
            "event_type=excluded.event_type, contradictions_json=excluded.contradictions_json, "
            "payload_json=excluded.payload_json, updated_at=excluded.updated_at",
            (
                story["story_id"], story["title"], iso(story["first_seen"]), iso(story["latest_seen"]),
                iso(story.get("first_public_at")), story["source_count"], story["publisher_count"], story["best_quality"],
                story["confirmation_status"], dumps(story.get("symbols", [])), story.get("event_type"),
                dumps(story.get("contradictions", [])), dumps(story.get("payload", {})),
                iso(datetime.now(timezone.utc)),
            ),
        )

    def stories(self, *, since: datetime | None = None, limit: int = 200) -> list[dict[str, Any]]:
        if since is None:
            rows = self._query("SELECT * FROM stories ORDER BY latest_seen DESC LIMIT ?", (limit,))
        else:
            rows = self._query(
                "SELECT * FROM stories WHERE latest_seen >= ? ORDER BY latest_seen DESC LIMIT ?", (iso(since), limit)
            )
        return [self._story_row(row) for row in rows]

    def story(self, story_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM stories WHERE story_id=?", (story_id,))
        return self._story_row(rows[0]) if rows else None

    @staticmethod
    def _story_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["symbols"] = json.loads(result.pop("symbols_json") or "[]")
        result["contradictions"] = json.loads(result.pop("contradictions_json") or "[]")
        result["payload"] = json.loads(result.pop("payload_json") or "{}")
        return result

    # ----------------------------------------------------------------- events
    def upsert_event(self, event: dict[str, Any]) -> None:
        now = iso(datetime.now(timezone.utc))
        self._execute(
            "INSERT INTO events(event_id, story_id, event_type, subtype, headline, symbols_json, direction, "
            "direction_confidence, direction_basis, materiality, materiality_score, novelty_status, novelty_score, "
            "modality, negated, source_quality, confirmation_status, public_at, first_seen, session_phase_at_event, "
            "payload_json, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(event_id) DO UPDATE SET story_id=excluded.story_id, event_type=excluded.event_type, "
            "subtype=excluded.subtype, headline=excluded.headline, symbols_json=excluded.symbols_json, "
            "direction=excluded.direction, direction_confidence=excluded.direction_confidence, "
            "direction_basis=excluded.direction_basis, materiality=excluded.materiality, "
            "materiality_score=excluded.materiality_score, novelty_status=excluded.novelty_status, "
            "novelty_score=excluded.novelty_score, modality=excluded.modality, negated=excluded.negated, "
            "source_quality=excluded.source_quality, confirmation_status=excluded.confirmation_status, "
            "payload_json=excluded.payload_json, updated_at=excluded.updated_at",
            (
                event["event_id"], event["story_id"], event["event_type"], event.get("subtype"), event["headline"],
                dumps(event.get("symbols", [])), event["direction"], float(event["direction_confidence"]),
                event.get("direction_basis"), event["materiality"], float(event["materiality_score"]),
                event["novelty_status"], float(event["novelty_score"]), event["modality"], int(bool(event["negated"])),
                event["source_quality"], event["confirmation_status"], iso(event.get("public_at")),
                iso(event["first_seen"]), event.get("session_phase_at_event"), dumps(event.get("payload", {})),
                now, now,
            ),
        )

    def events(self, *, since: datetime | None = None, symbol: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if since is not None:
            clauses.append("first_seen >= ?")
            params.append(iso(since))
        if symbol:
            clauses.append("symbols_json LIKE ?")
            params.append(f'%"{symbol.upper()}"%')
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(f"SELECT * FROM events {where} ORDER BY first_seen DESC LIMIT ?", (*params, limit))
        return [self._event_row(row) for row in rows]

    def event(self, event_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM events WHERE event_id=?", (event_id,))
        return self._event_row(rows[0]) if rows else None

    @staticmethod
    def _event_row(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        result["symbols"] = json.loads(result.pop("symbols_json") or "[]")
        result["payload"] = json.loads(result.pop("payload_json") or "{}")
        result["negated"] = bool(result["negated"])
        return result

    def replace_exposures(self, event_id: str, exposures: list[dict[str, Any]]) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM exposures WHERE event_id=?", (event_id,))
            for item in exposures:
                conn.execute(
                    "INSERT OR REPLACE INTO exposures(event_id, symbol, relationship, mechanism, expected_direction, "
                    "confidence, materiality, hop, payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        event_id, item["symbol"], item["relationship"], item["mechanism"], item["expected_direction"],
                        float(item["confidence"]), float(item["materiality"]), int(item["hop"]),
                        dumps(item.get("payload", {})),
                    ),
                )

    def exposures_for_event(self, event_id: str) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM exposures WHERE event_id=? ORDER BY hop, confidence DESC", (event_id,))
        return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]

    # ---------------------------------------------------------------- signals
    def upsert_signal(self, signal: StoredSignal) -> None:
        self._execute(
            "INSERT INTO signals(signal_key, symbol, event_id, story_id, direction, state, opportunity_score, "
            "first_seen_at, first_actionable_at, last_state_change_at, last_alert_at, last_alert_state, active, "
            "trade_date, payload_json, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(signal_key) DO UPDATE SET state=excluded.state, opportunity_score=excluded.opportunity_score, "
            "first_actionable_at=excluded.first_actionable_at, last_state_change_at=excluded.last_state_change_at, "
            "last_alert_at=excluded.last_alert_at, last_alert_state=excluded.last_alert_state, active=excluded.active, "
            "trade_date=excluded.trade_date, payload_json=excluded.payload_json, updated_at=excluded.updated_at",
            (
                signal.signal_key, signal.symbol, signal.event_id, signal.story_id, signal.direction, signal.state,
                float(signal.opportunity_score), iso(signal.first_seen_at), iso(signal.first_actionable_at),
                iso(signal.last_state_change_at), iso(signal.last_alert_at), signal.last_alert_state,
                int(signal.active), signal.trade_date, dumps(signal.payload), iso(datetime.now(timezone.utc)),
            ),
        )

    def load_signals(self, *, active_only: bool = True, trade_date: str | None = None) -> list[StoredSignal]:
        clauses = []
        params: list[Any] = []
        if active_only:
            clauses.append("active=1")
        if trade_date:
            clauses.append("trade_date=?")
            params.append(trade_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(f"SELECT * FROM signals {where} ORDER BY opportunity_score DESC", params)
        return [self._signal_row(row) for row in rows]

    def signals_for_symbol(self, symbol: str, limit: int = 50) -> list[StoredSignal]:
        rows = self._query(
            "SELECT * FROM signals WHERE symbol=? ORDER BY updated_at DESC LIMIT ?", (symbol.upper(), limit)
        )
        return [self._signal_row(row) for row in rows]

    @staticmethod
    def _signal_row(row: sqlite3.Row) -> StoredSignal:
        return StoredSignal(
            signal_key=row["signal_key"],
            symbol=row["symbol"],
            event_id=row["event_id"],
            story_id=row["story_id"],
            direction=row["direction"],
            state=row["state"],
            opportunity_score=float(row["opportunity_score"]),
            first_seen_at=parse_iso(row["first_seen_at"]) or datetime.now(timezone.utc),
            first_actionable_at=parse_iso(row["first_actionable_at"]),
            last_state_change_at=parse_iso(row["last_state_change_at"]) or datetime.now(timezone.utc),
            last_alert_at=parse_iso(row["last_alert_at"]),
            last_alert_state=row["last_alert_state"],
            active=bool(row["active"]),
            trade_date=row["trade_date"],
            payload=json.loads(row["payload_json"] or "{}"),
        )

    def deactivate_signals_before(self, trade_date: str) -> int:
        cursor = self._execute("UPDATE signals SET active=0 WHERE active=1 AND trade_date < ?", (trade_date,))
        return cursor.rowcount

    def record_transition(
        self, signal_key: str, symbol: str, from_state: str | None, to_state: str, at: datetime,
        score: float | None, reason: str,
    ) -> None:
        self._execute(
            "INSERT INTO signal_transitions(signal_key, symbol, from_state, to_state, at, opportunity_score, reason) "
            "VALUES(?,?,?,?,?,?,?)",
            (signal_key, symbol, from_state, to_state, iso(at), score, reason[:1000]),
        )

    def transitions(self, *, signal_key: str | None = None, since: datetime | None = None, limit: int = 500) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if signal_key:
            clauses.append("signal_key=?")
            params.append(signal_key)
        if since:
            clauses.append("at >= ?")
            params.append(iso(since))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(f"SELECT * FROM signal_transitions {where} ORDER BY id DESC LIMIT ?", (*params, limit))
        return [dict(row) for row in rows]

    # --------------------------------------------------------------- outcomes
    def upsert_outcome(self, outcome: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO outcomes(outcome_id, signal_key, event_id, symbol, event_type, direction, reference_kind, "
            "reference_at, reference_price, state_at_reference, opportunity_score_at_reference, actionable, "
            "market_state_json, returns_json, mfe, mae, time_to_peak_minutes, time_to_invalidation_minutes, "
            "became_exhausted, final_state, completed, trade_date, updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(outcome_id) DO UPDATE SET "
            "returns_json=excluded.returns_json, mfe=excluded.mfe, mae=excluded.mae, "
            "time_to_peak_minutes=excluded.time_to_peak_minutes, "
            "time_to_invalidation_minutes=excluded.time_to_invalidation_minutes, "
            "became_exhausted=excluded.became_exhausted, final_state=excluded.final_state, "
            "completed=excluded.completed, updated_at=excluded.updated_at",
            (
                outcome["outcome_id"], outcome["signal_key"], outcome["event_id"], outcome["symbol"],
                outcome.get("event_type"), outcome["direction"], outcome["reference_kind"], iso(outcome["reference_at"]),
                float(outcome["reference_price"]), outcome["state_at_reference"],
                outcome.get("opportunity_score_at_reference"), int(bool(outcome["actionable"])),
                dumps(outcome.get("market_state", {})), dumps(outcome.get("returns", {})), outcome.get("mfe"),
                outcome.get("mae"), outcome.get("time_to_peak_minutes"), outcome.get("time_to_invalidation_minutes"),
                int(bool(outcome.get("became_exhausted"))), outcome.get("final_state"),
                int(bool(outcome.get("completed"))), outcome["trade_date"], iso(datetime.now(timezone.utc)),
            ),
        )

    def outcomes(self, *, completed: bool | None = None, trade_date: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if completed is not None:
            clauses.append("completed=?")
            params.append(int(completed))
        if trade_date:
            clauses.append("trade_date=?")
            params.append(trade_date)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._query(f"SELECT * FROM outcomes {where} ORDER BY reference_at DESC LIMIT ?", (*params, limit))
        result = []
        for row in rows:
            item = dict(row)
            item["returns"] = json.loads(item.pop("returns_json") or "{}")
            item["market_state"] = json.loads(item.pop("market_state_json") or "{}")
            item["reference_at"] = parse_iso(item["reference_at"])
            item["actionable"] = bool(item["actionable"])
            item["completed"] = bool(item["completed"])
            item["became_exhausted"] = bool(item["became_exhausted"])
            result.append(item)
        return result

    def outcome_exists(self, outcome_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM outcomes WHERE outcome_id=?", (outcome_id,)))

    # -------------------------------------------------------- provider health
    def save_provider_health(self, provider_id: str, payload: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO provider_health(provider_id, payload_json, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(provider_id) DO UPDATE SET payload_json=excluded.payload_json, updated_at=excluded.updated_at",
            (provider_id, dumps(payload), iso(datetime.now(timezone.utc))),
        )

    def load_provider_health(self) -> dict[str, dict[str, Any]]:
        rows = self._query("SELECT provider_id, payload_json FROM provider_health")
        return {row["provider_id"]: json.loads(row["payload_json"]) for row in rows}

    # ---------------------------------------------------------------- issuers
    def replace_issuers(self, rows: list[dict[str, Any]], *, source: str) -> int:
        now = iso(datetime.now(timezone.utc))
        with self.transaction() as conn:
            for row in rows:
                conn.execute(
                    "INSERT INTO issuers(symbol, company_name, isin, industry, source, fetched_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(symbol) DO UPDATE SET company_name=COALESCE(excluded.company_name, issuers.company_name), "
                    "isin=COALESCE(excluded.isin, issuers.isin), industry=COALESCE(excluded.industry, issuers.industry), "
                    "source=excluded.source, fetched_at=excluded.fetched_at",
                    (row["symbol"], row.get("company_name"), row.get("isin"), row.get("industry"), source, now),
                )
        return len(rows)

    def issuers(self) -> dict[str, dict[str, Any]]:
        rows = self._query("SELECT * FROM issuers")
        return {row["symbol"]: dict(row) for row in rows}

    # -------------------------------------------------------- volume profiles
    def save_volume_profile(self, symbol: str, trade_date: str, buckets: dict[str, float], total: float, bars: int) -> None:
        self._execute(
            "INSERT INTO volume_profiles(symbol, trade_date, buckets_json, total_volume, bars) VALUES(?,?,?,?,?) "
            "ON CONFLICT(symbol, trade_date) DO UPDATE SET buckets_json=excluded.buckets_json, "
            "total_volume=excluded.total_volume, bars=excluded.bars",
            (symbol, trade_date, dumps(buckets), float(total), int(bars)),
        )

    def save_volume_profiles(self, trade_date: str, profiles: dict[str, tuple[dict[str, float], float, int]]) -> None:
        with self.transaction() as conn:
            for symbol, (buckets, total, bars) in profiles.items():
                conn.execute(
                    "INSERT INTO volume_profiles(symbol, trade_date, buckets_json, total_volume, bars) VALUES(?,?,?,?,?) "
                    "ON CONFLICT(symbol, trade_date) DO UPDATE SET buckets_json=excluded.buckets_json, "
                    "total_volume=excluded.total_volume, bars=excluded.bars",
                    (symbol, trade_date, dumps(buckets), float(total), int(bars)),
                )

    def volume_profiles(self, *, before_date: str, days: int = 20) -> dict[str, list[dict[str, float]]]:
        """Most recent `days` complete-day cumulative volume profiles per symbol, before `before_date`."""
        rows = self._query(
            "SELECT symbol, trade_date, buckets_json FROM volume_profiles WHERE trade_date < ? "
            "AND trade_date >= (SELECT COALESCE(MIN(d), '0000') FROM (SELECT DISTINCT trade_date AS d FROM volume_profiles "
            "WHERE trade_date < ? ORDER BY d DESC LIMIT ?)) ORDER BY trade_date DESC",
            (before_date, before_date, days),
        )
        result: dict[str, list[dict[str, float]]] = {}
        for row in rows:
            result.setdefault(row["symbol"], []).append(json.loads(row["buckets_json"]))
        return result

    # ------------------------------------------------------------ diagnostics
    def save_daily_diagnostics(self, trade_date: str, payload: dict[str, Any]) -> None:
        self._execute(
            "INSERT INTO daily_diagnostics(trade_date, payload_json, created_at) VALUES(?,?,?) "
            "ON CONFLICT(trade_date) DO UPDATE SET payload_json=excluded.payload_json, created_at=excluded.created_at",
            (trade_date, dumps(payload), iso(datetime.now(timezone.utc))),
        )

    def daily_diagnostics(self, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM daily_diagnostics ORDER BY trade_date DESC LIMIT ?", (limit,))
        return [{"trade_date": row["trade_date"], **json.loads(row["payload_json"])} for row in rows]

    # ------------------------------------------------------------------ stats
    def counts(self) -> dict[str, int]:
        tables = (
            "observations", "stories", "events", "exposures", "signals", "signal_transitions", "outcomes",
            "issuers", "volume_profiles", "daily_diagnostics",
        )
        result = {}
        for table in tables:
            result[table] = int(self._query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"])
        return result

    def outcome_statistics(self, *, min_sample: int = 30) -> dict[str, Any]:
        """Empirical outcome aggregates. Never presented as probabilities.

        Buckets below `min_sample` completed observations are reported with
        `sufficient_sample=False` and no averages, so nobody can mistake a
        handful of trades for a calibrated hit-rate.
        """
        rows = self._query(
            "SELECT event_type, state_at_reference, returns_json, mfe, mae FROM outcomes WHERE completed=1 AND actionable=1"
        )
        buckets: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            buckets.setdefault(f"{row['event_type'] or 'unknown'}|{row['state_at_reference']}", []).append(row)
        summary = {}
        for key, items in sorted(buckets.items()):
            event_type, state = key.split("|", 1)
            entry: dict[str, Any] = {
                "event_type": event_type,
                "state_at_reference": state,
                "completed_observations": len(items),
                "sufficient_sample": len(items) >= min_sample,
            }
            if len(items) >= min_sample:
                r30 = [json.loads(item["returns_json"]).get("30m") for item in items]
                r30 = [value for value in r30 if value is not None]
                entry["mean_directional_return_30m"] = round(sum(r30) / len(r30), 6) if r30 else None
                entry["share_positive_30m"] = round(sum(1 for value in r30 if value > 0) / len(r30), 4) if r30 else None
                mfe = [item["mfe"] for item in items if item["mfe"] is not None]
                mae = [item["mae"] for item in items if item["mae"] is not None]
                entry["mean_mfe"] = round(sum(mfe) / len(mfe), 6) if mfe else None
                entry["mean_mae"] = round(sum(mae) / len(mae), 6) if mae else None
            summary[key] = entry
        return {
            "note": "Empirical, uncalibrated outcome aggregates. Not probabilities; not a profitability claim.",
            "min_sample": min_sample,
            "buckets": list(summary.values()),
        }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def days_ago(days: float, *, now: datetime | None = None) -> datetime:
    return (now or utc_now()) - timedelta(days=days)
