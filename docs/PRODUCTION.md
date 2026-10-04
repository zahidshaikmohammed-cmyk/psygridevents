# PSYGRIDEVENTS production service

The production service turns public Indian market information plus PSYGRID's live 1-minute
OHLCV into a small, ranked, stateful list of event-driven opportunities:

```text
EVENT → STOCK → MECHANISM → MARKET REACTION → OPPORTUNITY → RANKED SIGNAL
```

It costs nothing beyond the Oracle Always Free VM that already runs PSYGRID: no premium news
vendor, no paid exchange feed and no second Dhan subscription.

* Start it with `python -m psygridevents.main --serve`; systemd does this in production.
* Live signals are served at `GET /signals/top`.
* Deployment steps are in [DEPLOYMENT_ORACLE.md](DEPLOYMENT_ORACLE.md) and the source list is in [SOURCES.md](SOURCES.md).

## 1. Architecture

```text
                ┌────────────── source loop (asyncio, per-provider schedule + backoff) ──────────────┐
 public sources │ NSE/BSE RSS + NSE CSV · SEBI · RBI · PIB · DGFT/MoSPI/CCI pages · Indian media RSS │
                │ Google News RSS · GDELT DOC 2.0           (probe → poll → health state)           │
                └──────────────────────────────┬──────────────────────────────────────────────────┘
                                               ▼
 NORMALIZE → DEDUPLICATE (content+URL keys, persisted) → FUSE into canonical STORY (evidence records)
   → RESOLVE ENTITIES (NSE filing link, official names, curated aliases, guarded symbols)
   → CLASSIFY (India taxonomy) → EXTRACT (SemanticExtractor: modality, negation, magnitude, effects)
   → MATERIALITY → NOVELTY → CONTRADICTION → DIRECTION → EXPOSURE GRAPH (989 stocks) → TRANSMISSION
                                               │  canonical events (SQLite)
                ┌────────── market loop (every minute at :06) ──────────┐
 PSYGRID        │ 22 × /public/live-{a..v}.json + nifty + indiavix +    │
 (same VM)      │ sectors.json → MarketSnapshot (compact arrays)        │
                └───────────────────────┬────────────────────────────────┘
                                        ▼
 for each (event, exposed stock): REACTION (3 clocks, RS, volume, VWAP, structure, exhaustion)
   → CP9 timing + CP9/CP10 exhaustion + CP11 SignalEngine → production policy (fail-closed)
   → opportunity_score → SignalBook (stateful, dwell/cooldown) → TopRanker (TOP 1-5, stable)
   → OutcomeTracker (1/5/15/30/60m, MFE/MAE) → publish → API / log / Telegram
```

| Module | Role |
|---|---|
| `providers/` | Source specs, adapters (RSS, Google News, GDELT, NSE CSV, listing pages), robots-aware HTTP, health states |
| `fusion.py` | Canonical stories with evidence records, confirmation status, contradictions |
| `entity_resolution.EntityResolver` | Guarded company/symbol resolution |
| `event_classifier.py` + `config/event_classification.yaml` | Deterministic India event taxonomy |
| `event_pipeline.EventBuilder` | Canonical event (reuses semantic, direction, materiality, novelty, contradiction engines) |
| `exposure_graph.ExposureGraph990` + `config/exposure_graph.yaml` | DIRECT / SECTOR / COMPETITOR / SUPPLIER / CUSTOMER / COMMODITY / REGULATORY / MACRO / SECOND_ORDER |
| `market_snapshot.py` | Bulk PSYGRID ingestion, VWAP/benchmark/sector context, regime |
| `reaction.py` | Live market confirmation + the three clocks + exhaustion |
| `opportunity.py` | Transparent `opportunity_score` |
| `live_engine.py` | Orchestrates everything per minute; production policy on top of CP11 |
| `signal_book.py` | State machine, alert de-duplication, TOP-N ranking |
| `outcomes.py` | Historical outcome memory |
| `storage.py` | SQLite (WAL) durable store |
| `runtime.py` / `api.py` / `alerts.py` | Service loops, JSON API + dashboard, delivery |
| `ai_assist.py` | Optional, budget-capped advisory AI (off by default) |

The legacy CP0–CP11 CLI (`--once`, `--watch`, `--health`) is unchanged. The production service
reuses its engines rather than replacing them.

## 2. Source hierarchy

Every observation carries one of four quality classes:

* `PRIMARY`: exchange disclosures.
* `OFFICIAL`: regulators and government.
* `REPUTABLE_SECONDARY`: established financial media.
* `DISCOVERY_ONLY`: Google News and GDELT.

A story's `confirmation_status` is one of:

| Status | Meaning |
|---|---|
| `CONFIRMED_PRIMARY` | At least one PRIMARY or OFFICIAL evidence record |
| `CORROBORATED` | At least 2 independent reputable publishers |
| `SINGLE_SOURCE` | One reputable publisher |
| `UNCONFIRMED_DISCOVERY` | Discovery-only evidence |
| `CONTRADICTED` | Open, unresolved source conflict |

Policy:

* Discovery-only and contradicted stories are capped at `discovery_only_max_score` (55, below the threshold). They can reach at most `EARLY_*`, and only when the stock's own market reaction is independently `confirmed`. They never reach `CONFIRMED`.
* A later PRIMARY or OFFICIAL filing resolves open contradictions in its favour. Every contradiction stays on record.

## 3. The three clocks

| Clock | Field(s) | Meaning |
|---|---|---|
| Event clock | `event_time`, `event_time_basis` | When the information became public. In order of preference: NSE dissemination timestamp from the filing link, then the source publication time, then first-seen. Future timestamps are rejected. |
| Market clock | `clocks.market_clock.reaction_start_at`, `event_to_price_latency_minutes` | When the stock started to react: the first bar beyond a volatility-scaled threshold, measured from the event baseline. |
| Opportunity clock | `remaining_opportunity`, `exhaustion_state` | How much tradeable movement plausibly remains: extension versus the stock's own 1-minute range, retracement from peak, distance from VWAP, fading momentum, volume climax, late session. |

**Market-available clock.** An event published outside market hours (overnight, weekend or
holiday) can only be reacted to from the next open. Freshness, CP9 timing and the old-event
penalty are therefore measured from that open (`freshness.market_available_at`), while
`event_time` still reports the true public time. A Friday-evening filing is FRESH at Monday 09:15,
not 63 hours stale.

The baseline depends on when the event became public:

* **During the session**: the last bar completed before the event.
* **Before the open**: the previous close, so the opening gap counts as the reaction.
* **After the close**: no live reaction is computed until the next session.

## 4. Signal states and transitions

`NO_SIGNAL · WATCH · EARLY_LONG · EARLY_SHORT · CONFIRMED · INVALIDATED · EXHAUSTED`

The CP11 `SignalEngine` makes the base decision: gates, exhaustion, market contradiction,
early versus confirmed. The production policy then only ever makes a signal **more conservative**:

* It applies the session phase.
* It requires LIVE market data.
* Indirect exposures need their own confirmed reaction.
* Discovery-only and contradicted evidence is capped.
* A direction inferred from the reaction itself is capped at EARLY.
* Signals must clear the score threshold.
* No new entries are allowed after 15:00 IST.

The `SignalBook` is a persistent state machine:

* Upgrades are immediate.
* Downgrades wait for `min_state_dwell_minutes`.
* `CONFIRMED` holds until it is invalidated or exhausted.
* `INVALIDATED` and `EXHAUSTED` are terminal for that event, stock and direction. Only a new event can start a new signal.
* Alerts fire only on entry into an actionable state, an upgrade, or the actionable→terminal step, with a per-signal cooldown. A stock never re-alerts every minute.

Signal key: `{event_id}:{symbol}:{direction}`. If a market-inferred direction flips, the old key is
invalidated and a new key starts.

## 5. `opportunity_score`

The score is a weighted sum (0–100) of transparent components, followed by multiplicative
penalties and caps. **It is not a probability.** The flag `score_is_probability: false` is on every
signal. Each signal carries `score_breakdown` (component value, weight and contribution,
penalties, caps) and `why` lines.

| Component | Weight | Component | Weight |
|---|---|---|---|
| event_materiality | 14 | market_reaction | 12 |
| remaining_opportunity | 10 | event_novelty | 8 |
| source_confidence | 8 | volume_confirmation | 8 |
| relative_strength | 7 | entity_confidence | 6 |
| direction_confidence | 6 | mechanism_confidence | 5 |
| vwap_confirmation | 5 | reaction_speed | 4 |
| sector_confirmation | 3 | market_regime | 2 |
| data_freshness | 2 | | |

| Penalty or cap | Value |
|---|---|
| Exhaustion | EXHAUSTED ×0.3, HIGH ×0.7 |
| Stale market data | ×0.4 |
| Zero volume in recent bars | ×0.5 |
| Open contradiction | ×0.6 |
| Indirect exposure | ×0.75 |
| Near close | ×0.8 |
| Event older than 6 h | ×0.6 |
| Routine or roundup item | ×0 |
| Discovery-only or contradicted | cap 55 |
| No established direction | cap 35 |

A huge but fully priced or ignored event therefore loses to a smaller, fresh, confirmed one. The
tests `test_large_event_without_price_reaction_is_not_actionable` and
`test_small_event_with_strong_reaction_outranks_large_event_without_reaction` cover this.

## 6. Signal schema (`GET /signals/top`)

```json
{
  "generated_at": "...", "phase": "MARKET", "min_opportunity_score": 60, "score_is_probability": false,
  "count": 1,
  "signals": [{
    "rank": 1, "symbol": "LT", "company": "Larsen & Toubro Limited",
    "signal_state": "CONFIRMED", "cp11_state": "CONFIRMED", "direction": "LONG",
    "direction_basis": "classification:order_win", "direction_confidence": 0.8,
    "opportunity_score": 91.7, "ranking_score": 91.7, "score_is_probability": false,
    "event": {"event_id": "...", "headline": "...", "type": "order", "subtype": "order_win",
              "magnitude_crore": 2500, "materiality": "high", "novelty": "new",
              "confirmation_status": "CONFIRMED_PRIMARY", "contradiction_status": "none"},
    "event_time": "2026-10-05T05:00:00+00:00", "event_time_basis": "nse_dissemination_timestamp",
    "source": {"best_quality": "PRIMARY", "publishers": ["nse corporate announcements", "economic times"], "source_count": 2},
    "market_reaction": {"move_since_event": 0.034, "baseline_kind": "pre_event_bar", "response_state": "developing",
                        "confirmation": "confirmed", "reaction_start_at": "...", "event_to_price_latency_minutes": 2.0,
                        "persistence": 0.95, "retracement_from_peak": 0.02, "momentum_5m": 0.006, "gap": 0.002},
    "price": 3620.5, "intraday_return": 0.031, "day_change": 0.035,
    "relative_strength": {"label": "Strong", "value": 0.031, "basis": "index:nifty", "sector": "CAPITAL_GOODS_ENGINEERING", "sector_relative": 0.027},
    "volume_confirmation": {"ratio": 3.1, "basis": "post_event_vs_pre_event_median_1m", "confirmed": true},
    "VWAP_state": "above", "vwap": 3561.2, "exhaustion_state": "LOW", "exhaustion_flags": [],
    "remaining_opportunity": 1.0,
    "clocks": {"event_clock": {}, "market_clock": {}, "opportunity_clock": {}},
    "freshness": {"source_timestamp": "...", "observed_timestamp": "...", "ingestion_latency_seconds": 31.0,
                  "market_data_age_seconds": 18.4, "event_age_seconds": 261, "event_age": "4m 21s",
                  "freshness_status": "FRESH"},
    "mechanism": {"relationship": "DIRECT", "mechanism": "explicitly_named_in_event", "hop": 0, "confidence": 0.99},
    "why_now": "Fresh high materiality order win, ₹2,500 cr + price confirmation +3.40% since event + abnormal volume 3.1x baseline + ...",
    "invalidation": "Below 3561.20 (max(event baseline, VWAP)); or a contradicting primary-source update; or exhaustion ...",
    "invalidation_price": 3561.2, "risk_flags": [], "source_urls": ["https://nsearchives.nseindia.com/corporate/LT_..."],
    "score_breakdown": {}, "policy_notes": [], "uncertainty": []
  }]
}
```

`GET /signals/top?format=text` returns the operator format: `PSYGRID EVENT SIGNAL / RANK #1 /
SYMBOL / DIRECTION / STATE / OPPORTUNITY SCORE / EVENT / EVENT AGE / MARKET / RELATIVE STRENGTH /
VOLUME / VWAP / EXHAUSTION / WHY NOW / INVALIDATION / SOURCE`.

### API

All endpoints are `GET` and return JSON. Add `?token=` or `Authorization: Bearer` if
`PSYGRIDEVENTS_API_TOKEN` is set; `/health` is always open.

| Endpoint | Content |
|---|---|
| `/health` | Liveness: OK / STARTING / DEGRADED (200) or ERROR (503) |
| `/providers` | Per-source health: state, last success, errors, latency, resolved URL, verification |
| `/events?since_hours=&symbol=&limit=`, `/events/latest` | Canonical events |
| `/stories?since_hours=` | Stories with every evidence record |
| `/signals?state=` | All active signals of the trade date |
| `/signals/top[?format=text]` | TOP 1–5 above the quality threshold |
| `/signals/{symbol}` | Active signals, state history and events for one stock |
| `/market/health` | PSYGRID status, shard health, coverage, bar age, regime (breadth, NIFTY, VIX) |
| `/system/status` | Phase, versions, redacted settings, AI/Telegram status, DB counts |
| `/metrics[?format=prometheus]` | Counters and gauges |
| `/outcomes`, `/outcomes/stats` | Outcome memory and empirical aggregates (no probability below 30 samples per bucket) |
| `/diagnostics/daily` | Post-close diagnostics |
| `/transitions?since_hours=` | Signal state transitions |
| `/`, `/dashboard` | Minimal browser dashboard (auto-refresh 15 s) |

## 7. Database

The database is `data/psygridevents.sqlite3` (SQLite in WAL mode, `synchronous=NORMAL`). The
schema version is stored in `PRAGMA user_version`, and the service refuses to run against a newer
schema.

| Table | Content |
|---|---|
| `observations` | Every raw evidence record (title, URL, quality, timestamps, ingestion latency). Raw payloads are pruned after 45 days; evidence rows are kept. |
| `stories` | Canonical stories: counts, best quality, confirmation status, contradictions |
| `events` | Canonical events, with the full payload and a minimal semantic record for novelty |
| `exposures` | The exposure graph output per event |
| `signals`, `signal_transitions` | Stateful signal book and the audit trail |
| `outcomes` | 1/5/15/30/60-minute directional returns, MFE, MAE, time to peak/invalidation, exhaustion |
| `provider_health` | Survives restarts |
| `issuers` | Official names, ISINs and industries from NSE CSVs |
| `volume_profiles` | 15-minute cumulative volume per stock per day, for historical RVOL |
| `daily_diagnostics` | Post-close report: missed, false and late signals |
| `kv` | Daily-routine markers, TOP list, issuer refresh time |

Backup: `deploy/backup_db.sh` runs daily from cron, makes an online-consistent copy and keeps 14.

## 8. Restart recovery

On start, `LiveEngine.restore()` reloads:

* stories from the last 72 hours with all their evidence;
* events and novelty history from the last 7 days;
* active signal states for the trade date;
* open outcomes;
* the dedupe keys of the last 3 days;
* the previous TOP list, for rank stability.

Re-polled feed items are duplicates. An unchanged signal is never re-announced
(`test_restart_recovers_state_without_reannouncing`). Provider counters survive restarts, but
each source must pass a fresh probe before it is used again.

## 9. Daily operation (IST)

| Time | What happens |
|---|---|
| 00:00–08:00 | Sources polled slowly (off-hours interval); market loop every 5 minutes (PSYGRID reports CLOSED) |
| 08:00 PRE_MARKET | Failed sources re-probed, official issuer names refreshed, PSYGRID `/health` checked, historical volume profiles loaded. Fast polling begins. Overnight and pre-open events are scored as WATCH; no live entries yet. |
| 09:15 MARKET | Minute loop: bulk snapshot, reaction for every exposed stock, signals, TOP 1–5, alerts. Pre-open events are judged against the previous close (gap). |
| 15:00 NEAR_CLOSE | No new EARLY/CONFIRMED entries; existing signals keep updating; `late_session` exhaustion flag |
| 15:30 POST_MARKET | Outcomes completed, volume profiles saved, daily diagnostics written (missed, false and late signals), old raw payloads pruned |
| Weekend / configured holiday | NON_TRADING_DAY: acquisition continues, no live signals |

## 10. Failure modes

| Failure | Behaviour |
|---|---|
| One source down, blocked or changed | That provider moves ERROR → DISCONNECTED with exponential backoff and is re-probed every 6 h and before each session. Others continue. |
| All news sources down | Engine runs; `/health` reports DEGRADED after 5 minutes; no new events |
| robots.txt disallows | Source becomes UNAVAILABLE (never fetched); an unreachable robots.txt is a transient error |
| PSYGRID unreachable | Market health DISCONNECTED; every actionable state is held at WATCH; no fabricated reaction |
| Some PSYGRID shards fail | DEGRADED; stocks in good shards still evaluated |
| Stale candles (age over 150 s) | Symbol STALE: fail closed (WATCH) with a `stale_market_data` risk flag |
| Future-dated or malformed candle | Rejected and counted per symbol |
| Zero volume (halt or circuit) | `zero_volume_recent_bars` flag, ×0.5 penalty |
| PSYGRID universe changes | `config/instruments.json` must be resynced (`tools/sync_universe_from_psygrid.py`); the loader fails closed on a mismatched count |
| Duplicate, roundup or routine filings | Deduplicated, or kept as evidence but never signalled |
| Contradictory sources | Recorded; confidence ×0.5 and score ×0.6 until a primary source resolves it |
| AI or Telegram failure | Logged; the engine is unaffected |
| Crash or OOM | systemd restarts in 5 s; state restored from SQLite |
| Corrupt database | Integrity check at start-up refuses to run; restore the latest file from `data/backups/` |

## 11. Troubleshooting

| Symptom | Check |
|---|---|
| `curl -s localhost:10100/health` fails | `systemctl status psygridevents` and `journalctl -u psygridevents -n 200` |
| No events | `curl -s localhost:10100/providers \| jq '.providers[] \| {provider_id,state,last_error}'`. If NSE shows DISCONNECTED with HTTP 403, NSE is blocking the VM's IP; the other sources keep working. |
| No signals during market hours | `curl -s localhost:10100/market/health`. `health` must be HEALTHY and `latest_bar_age_seconds` under 150. Then `curl -s 'localhost:10100/signals?state=WATCH'` shows candidates and their `policy_notes`. |
| Market health CLOSED on a weekday | PSYGRID reports CLOSED: a holiday, or PSYGRID's own session is not live |
| Too few or too many TOP signals | Tune `PSYGRIDEVENTS_MIN_OPPORTUNITY_SCORE` (default 60) |
| Wrong company matched | Add a blocked phrase or curated alias in `config/issuer_aliases.yaml`, or refresh issuers with `python -m psygridevents.main --refresh-issuers` |
| See which sources actually work from the VM | `.venv/bin/python -m psygridevents.main --probe-sources` |

## 12. Known limitations

These are stated plainly; no capability is claimed beyond them.

* **Free sources are not a wire service.** RSS publication lags of seconds to minutes are normal, GDELT updates about every 15 minutes, and NSE/BSE may block cloud IPs. The probe results on the VM show which feeds actually work.
* **NSE RSS item format.** NSE feed titles carry the company name; the event subtype comes from the description, the filing subject in the link, and the classifier. Filings whose substance exists only inside the PDF (for example "Outcome of Board Meeting") are classified generically and rely on the market reaction for direction. PDFs are not parsed.
* **Historical RVOL.** This needs stored daily volume profiles; it becomes available after about 3 trading days. Until then, volume confirmation uses the intraday pre-event baseline, or is reported unavailable for pre-open events.
* **Supplier and customer edges.** These are empty until documented relationships are added to `config/exposure_graph.yaml`; nothing is guessed.
* **No calibrated probabilities.** The outcome memory collects the data for later calibration; `/outcomes/stats` shows aggregates only above 30 samples per bucket.
