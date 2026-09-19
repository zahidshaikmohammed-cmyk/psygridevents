# Operations — health, persistence, market state, CLI reference

## CLI reference

```bash
python -m psygridevents.main                          # diagnostics only
python -m psygridevents.main --once                    # one pipeline run, human-readable
python -m psygridevents.main --once --json              # one pipeline run, CP7 JSON contract
python -m psygridevents.main --once --limit N            # cap human-readable ranked output
python -m psygridevents.main --once --market-data none    # force NullMarketDataAdapter (offline/CI)
python -m psygridevents.main --once --state-file PATH      # opt-in incremental/idempotent --once
python -m psygridevents.main --watch --interval SECONDS     # continuous polling mode
python -m psygridevents.main --health                        # diagnostic status snapshot, no acquisition
```

`--json` requires `--once` and is incompatible with `--watch`. `--health`
takes precedence over everything else and never runs acquisition.

## Market state vs. Psygrid availability (section 4)

These are two different facts and the engine never conflates them:

| State | Meaning | Source |
|---|---|---|
| `MARKET_OPEN` | Psygrid reports an active (`LIVE`) session. | Psygrid's own `/public/live.json` `status` field. |
| `MARKET_CLOSED` | Psygrid reports the session as `CLOSED`. | Same field. |
| `MARKET_DATA_UNAVAILABLE` | Psygrid is unreachable, or reachable but reporting something other than `OK`/`CLOSED` (e.g. `AUTH_ERROR`, `CONFIG_ERROR`). | `psygrid_client.py::PsygridClient._map_market_state`. |
| `UNKNOWN` | No live market-data adapter was consulted at all this run (e.g. `--market-data none`, or no event resolved any asset). | `signal_engine.py` default. |

**Caveat, observed not assumed**: Psygrid's own `session.py::in_market()`
checks only the configured time-of-day window, not the day of week, so
`MARKET_OPEN` reflects Psygrid's own self-report and can be true on a
non-trading weekend. `market_data.freshness`/`universe_coverage()`'s
`live_data_received` — not session state alone — is the authoritative
signal that real ticks are actually flowing.

Every `SignalAssessment` carries `market_session_state`,
`market_observation_timestamp`, and `market_data_freshness`
(`LIVE`/`STALE`/`NO_DATA`/`TIME_ERROR`, derived from the already-fetched
observation, never a new network request). None of these change the
signal-state decision rules — they are diagnostic fields required by the
delivery contract (section 8), not additional gates.

Per section 4's explicit list, when the market is closed: event acquisition,
semantic extraction, universe verification, configuration validation,
pipeline integrity checks, and offline replay all continue to operate
normally. Only market confirmation/response stay `untested`/`no_response`,
and no live directional signal (`EARLY_LONG`/`EARLY_SHORT`/`CONFIRMED`) is
produced without genuine market observations backing it.

## Health / status (section 9)

```bash
python -m psygridevents.main --health
```

Prints a JSON `HealthReport` (`src/psygridevents/health.py`) and exits `1`
only if the canonical universe itself is unavailable — never for a market
that is closed or a Psygrid outage, both of which are reported as
`DEGRADED`, not `UNAVAILABLE`. Health is diagnostic only: nothing in
`health.py` feeds back into a signal decision.

Fields: `application_status` (`OK`/`DEGRADED`/`UNAVAILABLE`), universe
status/count/error, per-provider status (current run, or the last known
value persisted by `--state-file` if the provider wasn't attempted this
run), market connectivity/raw status, and (when run alongside a pipeline
execution) per-stage pipeline counts — raw observations, stories, semantic
events, entity-resolved, materiality-assessed, asset-resolved,
market-tested, untested, and a breakdown of signals by state.

## Persistence / idempotency (section 7)

The smallest mechanism that satisfies "safe restart, no duplicate
publication": one local JSON file (`state_store.py::PublicationStateStore`),
no database. It tracks:

- the acquisition watermark (`since`) — a restarted process resumes
  incremental acquisition instead of reprocessing everything;
- the last-published `signal_state` per (deterministic) `event_id` — a
  restarted process never re-announces an unchanged signal as brand new;
- per-provider last-attempt/last-success/last-error, for `--health`.

Writes are atomic (temp file + rename); a state file that fails to parse is
treated as empty rather than crashing (losing the watermark degrades to
"reprocess a bit more", never a fabrication or a crash).

`--watch` always uses a state file (default `.psygridevents_state.json`,
override with `--state-file`). `--once` is stateless by default (fully
reproducible per invocation, matching its original contract) and only
becomes incremental/idempotent when `--state-file` is passed explicitly.

## Provider failure isolation (section 5)

`StoryEngine.acquire()` isolates each feed: one provider raising any
exception (timeout, connection failure, HTTP error, malformed content) is
recorded on `engine.last_acquisition_status[provider_id]` and does not
prevent the other configured feeds from being acquired in the same call.
See `tests/test_acquisition_isolation.py` and `tests/test_acquisition.py`.

## Performance (section 12)

`deduplication.py::deduplicate()` was measurably the dominant cost at
realistic batch sizes (profiled: ~11s for 1,218 observations, matching the
historically observed live-run volume). The fix is provably
behavior-preserving (verified by differential testing against the original
algorithm across 2,000+ random cases, see `tests/test_deduplication.py`):
`canonical_text(title)` is computed once per observation instead of once
per comparison, and `difflib.SequenceMatcher.ratio()` — expensive — is
skipped whenever its exact mathematical upper bound
(`2*min(len_a,len_b)/(len_a+len_b)`, since it can never match more
characters than the shorter string contains) already falls below the
similarity threshold. This does not change which observations are
classified as duplicates.
