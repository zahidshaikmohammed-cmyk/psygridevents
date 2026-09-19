# Live Market-Data Integration — psygridevents consumes Psygrid's live feed

## Step 1 — What Psygrid actually does (inspected, not assumed)

Psygrid (`zahidshaikmohammed-cmyk/Psygrid`) is a FastAPI service (`app.py`)
that:

- Resolves each of its 990 symbols to a Dhan security ID **once per process
  start** via `instrument_master.py::fetch_nse_equity_security_ids()`,
  which fetches Dhan's own instrument-master CSV
  (`https://images.dhan.co/api-data/api-scrip-master.csv`) and matches on
  `SEM_EXM_EXCH_ID=NSE` + `SEM_INSTRUMENT_NAME=EQUITY`. IDs are **never
  persisted** (Psygrid's own "RAM only" policy) — a restart re-resolves them.
  There is no fixed refresh schedule beyond "on every process
  start/deploy" (Oracle deploys restart the `psygrid` systemd service on
  every push to `main`).
- Authenticates against Dhan (`dhan_auth.py`, TOTP-based token generation)
  and manages daily session boundaries (`session.py::SessionManager`,
  09:15–15:15 IST).
- Opens **one** Dhan WebSocket (`feed.py`/`feed_runtime.py::LiveFeed`) for
  all 990 instruments, forms 1-minute OHLCV candles in RAM
  (`state.py::PsygridState.update_quote`), and handles reconnects with a
  health-check thread that resubscribes stale instruments (cooldown 30s)
  and falls back to a Dhan REST quote snapshot after 5s of staleness
  (`feed_runtime.py::LiveFeed._health_pass`).
- Exposes the result as **plain HTTP JSON, symbol-keyed**, not security-ID
  keyed: `/public/live.json` (all 990), `/public/live-{a..v}.json` (22
  disjoint shards of 45), `/public/stock/{symbol}.json` (one symbol). Every
  stock record is `{"symbol", "security_id", "previous_close", "today_open",
  "candles_1m": [{"timestamp", "open", "high", "low", "close", "volume"}]}`
  (`output.py`). There is **no WebSocket exposed to external consumers** —
  only `Cache-Control: no-store` JSON meant to be polled.
- Additionally exposes derived indicators (MA9/EMA20/RSI14/VWAP) via a
  **separate** `/public/indicators*.json` family (`psygrid_master_indicator.py`,
  `indicator_runtime.py`), computed from the same 1-minute candles. VWAP
  exists there, not in the raw OHLCV contract.
- Both `/public/live.json` and the indicator layer already implement a
  freshness concept: compare each observation's timestamp against the
  payload's own reported clock (`session.current_time_ist`) using a
  configurable staleness threshold, and explicitly flag a candle that is
  *ahead* of that clock as a time error
  (`psygrid_master_indicator.py::_freshness`). psygridevents' own freshness
  handling below mirrors this exact idea rather than inventing a different one.

**Conclusion for Step 2 (integration boundary):** `/public/stock/{symbol}.json`
and `/public/live.json` already form a stable, machine-readable,
symbol-keyed contract that contains everything CP10 strictly requires
(symbol, timestamp, open, high, low, close, volume) except VWAP/benchmark/
sector — fields CP10/CP11 already treat as optional and nullable. Consuming
this HTTP contract requires **no** Dhan credentials, **no** second
WebSocket, and **no** independent security-ID resolution: Psygrid already
did that and expressed the result by plain symbol. Per the mandated
preference order, **Option 1 applies** — reuse the existing contract as-is.
Nothing needed to be added to Psygrid.

## Step 3 — `MarketDataAdapter` implementation

- `src/psygridevents/psygrid_client.py` — `PsygridClient`, a thin read-only
  HTTP client for exactly three endpoints
  (`/public/stock/{symbol}.json`, `/public/live.json`, `/health`).
  `parse_ist_timestamp()` reimplements Psygrid's own
  `"YYYY-MM-DD HH:MM:SS IST"` parsing (same format as
  `psygrid_master_indicator.py::_parse_ist_timestamp` and
  `tools/check_live_universe.py`) without a pandas dependency.
- `src/psygridevents/market_data.py::PsygridMarketDataAdapter` — implements
  the existing `MarketDataAdapter` ABC. `NullMarketDataAdapter` is
  unchanged and remains the explicit fail-closed choice for tests/offline
  use (`--market-data none`).
- Fields populated: `symbol`, `timestamp` (parsed IST → UTC), `open`,
  `high`, `low`, `close`, `volume` — all genuinely from Psygrid. `vwap`,
  `average_volume`, `benchmark_return`, `sector_return` are left `None`
  (Psygrid's OHLCV contract does not carry them; VWAP is available in
  Psygrid's separate indicators endpoint but is **not** wired in here —
  see "Explicitly out of scope" below). CP10/CP11 already treat every one
  of these as optional and report `"unavailable"` rather than fabricating
  a value, so this required no CP10/CP11 change.

## Step 4 — 990 coverage

`PsygridMarketDataAdapter.universe_coverage(configured_symbols, as_of)` (backed
by `PsygridClient.universe_coverage`) makes **one** `/public/live.json`
request and reports, for the full configured universe:

```
configured = <len(psygridevents' 990-symbol universe)>
resolved_security_ids = <count with a non-empty security_id in Psygrid's response>
live_data_received   = <count whose latest candle is within the freshness threshold>
stale                = <count whose latest candle exists but is older than the threshold>
missing              = <count absent from Psygrid's response entirely>
```

`main.py --once` prints this line (`Live market-data coverage: ...`) after
every run when the Psygrid adapter is active. The universe itself is never
silently reduced: this is a read-only diagnostic over the full configured
990, not a filter applied to it.

## Step 5 & 6 — Freshness and the real-time boundary

- **Future rejection**: `PsygridClient._parse_candles` drops (and counts)
  any candle whose parsed timestamp is after `as_of`, at the adapter
  boundary, in addition to CP10's own `timestamp <= as_of` filtering — the
  boundary is enforced twice, deliberately (see
  `tests/test_market_data.py::test_psygrid_adapter_real_time_boundary_rejects_future_observation`
  and `tests/test_psygrid_client.py::test_future_candle_relative_to_as_of_is_rejected`).
- **Malformed rejection**: a candle with an unparsable timestamp, a
  non-numeric/non-positive price, or a negative volume is dropped and
  counted (`FetchDiagnostics.malformed_count`), never coerced into a guess.
- **Staleness**: `PsygridMarketDataAdapter.freshness(symbol, as_of, max_age_seconds=120)`
  reports `LIVE` / `STALE` / `NO_DATA` / `UNAVAILABLE` / `TIME_ERROR` from
  the age of the *latest* observation relative to `as_of` — mirroring
  Psygrid's own `state_runtime.py::RuntimeFreshnessState.freshness()`
  contract (same status vocabulary, same "age vs. configurable threshold"
  shape) so "live vs. stale" means the same thing in both systems.
  Historical intraday candles are not deleted because they are old — they
  are genuine, non-fabricated past observations CP10 needs for baseline and
  displacement; staleness is a property of *the latest tick*, exactly as
  Psygrid itself treats it, not a filter over history.

## Step 7 — Event-to-market attachment stays event-driven

`main.py::run_pipeline` resolves CP8 (`build_asset_mechanism`) **before**
touching the market-data adapter, then calls
`market_data.observations(asset, as_of=now)` **only** for assets a real
event actually resolved to (via `AssetMechanismMapping.resolved`). This was
already the shape of the code from the CP8–CP11 milestone; it required no
change here. A single event never triggers more than one
`/public/stock/{symbol}.json` request, and the full 990-symbol universe is
never scanned per event.

## Step 8 — Complete signal chain, verified

`EVENT -> ASSET -> MECHANISM -> EXPECTED DIRECTION -> LIVE MARKET RESPONSE
-> EXHAUSTION -> CP11 SIGNAL` was exercised end-to-end
(`tests/test_main_pipeline.py`, `tests/test_signal_pipeline.py`) using both
`NullMarketDataAdapter` (→ `WATCH`, no market evidence) and synthetic
observations shaped exactly like `PsygridMarketDataAdapter`'s output (→
`EARLY_LONG`). `MarketConfirmationEngine` (CP5) is untouched and still feeds
the `CONFIRMED`/`INVALIDATED` gates. `signal_strength_or_confidence` still
never reads `priority_score` (unchanged from CP11; `SignalEngine.assess` has
no priority parameter).

## Step 9 — Runtime modes

`--once` is unchanged in shape (same output, same JSON contract) — its
internals were refactored into a shared `run_pipeline()` function, used
identically by the new `--watch` mode. `--watch --interval N` polls
(default 60s): acquire → resolve → observe → update timing/response/
exhaustion/signal → print only the signals whose state actually changed
since the previous poll. Psygrid does not expose a push/streaming channel
externally (its own WebSocket is internal to itself; its public contract is
explicitly `Cache-Control: no-store` HTTP meant to be polled), so a bounded
poll loop is the correct minimal client for that contract, not an invented
complication.

## Step 10 — Error handling (fail-closed, verified live in this environment)

| Failure                                   | Behavior                                                                 |
|--------------------------------------------|---------------------------------------------------------------------------|
| Psygrid/Dhan unavailable (network/HTTP error) | `observations()` returns `()`; diagnostics record the error; signal degrades to `WATCH`/`NO_SIGNAL`, never fabricated. |
| Non-200 / `CONFIG_ERROR` / `STARTING`     | Treated as unavailable, `()`.                                            |
| Missing symbol (`status: NOT_FOUND`)      | `()`, recorded as `"symbol not found in Psygrid universe"`.              |
| Malformed candle / timestamp              | That candle dropped and counted; the rest of the series is still used.  |
| Future observation                        | Dropped at the adapter boundary and by CP10.                             |
| Partial 990 coverage                      | Reported via `universe_coverage()`, never silently narrowed.            |
| Event without a resolvable asset          | CP8 already yields `resolved=False`; CP11 already gates this to `NO_SIGNAL` (unchanged). |

This was verified for real, not only in mocked tests: this development
session has no network route to the actual Oracle host, so running
`python -m psygridevents.main --once` here (against the real default
`http://140.245.226.102:10000`) produces a genuine `ConnectTimeout`, and the
pipeline correctly reports `Live market-data coverage:
CANONICAL_MARKET_DATA_UNAVAILABLE (ConnectTimeout: ...)` and still emits a
correct `WATCH` signal rather than crashing or fabricating a reaction.

## Step 11 — Tests

- `tests/test_psygrid_client.py` (19 tests): timestamp parsing, the real
  Psygrid response schema (see "How the fixtures were made" below),
  `NOT_FOUND`, HTTP errors, network failure, malformed JSON, future-candle
  rejection, malformed-field rejection, and `universe_coverage` counting
  (resolved/live/stale/missing).
- `tests/test_market_data.py` (added): `PsygridMarketDataAdapter`
  observations, fail-closed on outage, freshness LIVE/STALE/NO_DATA, and
  the real-time boundary at the adapter level.
- `tests/test_main_pipeline.py`: adapter selection from CLI args, and full
  `run_pipeline()` runs producing `WATCH` (no data) and `EARLY_LONG`
  (synthetic data matching the real contract shape).
- All pre-existing tests (CP0–CP11, universe integration) remain green.

### How the fixtures were made

`tests/fixtures/psygrid_stock_response_sample.json` and
`psygrid_stock_not_found_sample.json` were generated by importing Psygrid's
own `state.py::PsygridState` and `output.py::stock_json` directly (the same
approach Psygrid's own `tests/test_universe_contract.py` uses) against a
synthetic in-memory state, then dumping the real return value — not
hand-typed. This proves the parser in `psygrid_client.py` matches Psygrid's
actual serialization code, not an assumption about its shape.

## Step 12 — Real integration status

- **Against the actual Psygrid repository**: done. The response-schema
  fixtures above were generated from Psygrid's real code
  (commit `484e482fc71a2a9e0f122f06d783925acb7c98d7`), and the parser was
  tested against them.
- **Against the real Oracle environment** (`http://140.245.226.102:10000`):
  attempted for real, not skipped. This development session has no network
  route to that host at all (confirmed: TCP connections to both port 22 and
  port 10000 on `140.245.226.102` time out from this environment, the same
  restriction observed during the CP8–CP11 milestone). Running
  `psygridevents.main --once` with the real default URL genuinely attempts
  the connection, times out, and the pipeline correctly reports
  `CANONICAL_MARKET_DATA_UNAVAILABLE` — this proves the fail-closed path
  works, but it is **not** a substitute for validating actual live data
  content against a reachable Psygrid instance during NSE market hours.
  That validation needs to run from an environment with network access to
  Oracle (e.g., the Oracle host itself, or CI once it is granted access),
  using `python -m psygridevents.main --once` (default `--market-data
  psygrid`) and checking the printed coverage line and any live signals.

## Explicitly out of scope here

- VWAP enrichment from Psygrid's separate `/public/indicators/{symbol}.json`
  endpoint is a straightforward, isolated future addition (same client,
  one more endpoint, same fail-closed shape) but was not wired in, since
  CP10/CP11 already function correctly with `vwap=None` and adding it now
  would be scope creep beyond what this milestone asked for.
- Dhan security-ID resolution, WebSocket handling, and reconnect logic are
  not reimplemented in psygridevents — Psygrid owns all of that, by design.
- No changes were made to CP0–CP11's architecture or state machine.
