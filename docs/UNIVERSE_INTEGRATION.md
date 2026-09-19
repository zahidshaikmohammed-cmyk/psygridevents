# Universe Integration — psygridevents consumes Psygrid's canonical universe

## Ownership

`zahidshaikmohammed-cmyk/Psygrid` is the single canonical owner of the live
990-stock NSE equity universe. psygridevents does **not** maintain an
independent universe definition; it vendors a synced, provenance-stamped
copy and enforces the same strict contract Psygrid enforces on itself.

## The canonical source

- **Repository**: `zahidshaikmohammed-cmyk/Psygrid`
- **File**: `stocks.json` (repo root)
- **Schema**: `{"universe": "PSYGRID_990", "exchange": "NSE", "instrument": "EQUITY", "symbols": [...990 plain NSE trading symbols...]}`
- **Validated by Psygrid itself** in `config.py::_load_symbol_universe()`
  (`UNIVERSE_SIZE = 990`, exact count, uniqueness, exchange/instrument checks)
  and in `tests/test_universe_contract.py::test_canonical_stock_universe_is_exactly_990_and_unique`.
- Symbols are plain uppercase NSE trading symbols as strings (e.g.
  `RELIANCE`, `M&M`, `BAJAJ-AUTO`); some contain `&`/`-`. There is **no**
  identifier (security ID) in `stocks.json` itself — Dhan security IDs are
  resolved live, in RAM only, via `instrument_master.py::fetch_nse_equity_security_ids()`
  against Dhan's own instrument-master CSV, and are never persisted. This
  means `stocks.json` is the right (and only) thing to sync for a *symbol*
  universe; security IDs are a live-market-data concern, not a universe
  concern, and belong to the future CP10 live adapter, not this sync.
- Psygrid's live JSON endpoints (`/public/live.json`,
  `/public/live-{shard}.json`) embed the symbol list too, but **only when
  the service is running during NSE market hours** (09:15–15:15 IST) — off
  market hours they report zero records. `stocks.json` is therefore the
  only universe source that is available at all times, which is why it is
  the integration point, not the live endpoints.

## Why vendor a copy instead of fetching live on every run

CP0–CP11 are deterministic and offline-testable by design (no pipeline run
depends on live network access). Making every `--once` invocation depend on
live GitHub/Psygrid availability would break that property and make the
existing test suite non-deterministic. Instead:

- `config/instruments.json` is a **vendored, provenance-stamped mirror** of
  Psygrid's `stocks.json`, updated only by `tools/sync_universe_from_psygrid.py`.
- Every normal run (`universe.load_instruments()`) validates the vendored
  file against the exact canonical contract (990 unique, well-formed
  symbols) and **fails closed** — raising `CanonicalUniverseUnavailableError`
  (message prefixed `CANONICAL_UNIVERSE_UNAVAILABLE`) — if it does not. This
  means a corrupted, reverted, or hand-edited file (e.g. someone reverting
  to the historical 450) is rejected immediately rather than silently used.
- A **separate, explicit** live-equality check
  (`tools/verify_universe_against_psygrid.py`) is how an operator or CI
  proves, on demand, that the vendored copy still matches Psygrid's actual
  current `stocks.json` — mirroring Psygrid's own operational pattern
  (`tools/check_live_universe.py`).

## Files

- `config/instruments.json` — vendored universe. New `canonical_source`
  object records `repo`, `path`, `universe_id` (`PSYGRID_990`), `commit`
  (the Psygrid commit the sync was taken from), and `synced_at`. The
  `instruments` array (990 symbols, Psygrid's original order) is unchanged
  in shape from before, so every existing consumer
  (`entity_resolution.InstrumentResolver`, `issuer_master.IssuerMasterBuilder`,
  `universe.load_instruments`) needed **no changes**.
- `src/psygridevents/universe_integrity.py` — `EXPECTED_UNIVERSE_SIZE = 990`,
  `CanonicalUniverseUnavailableError`, `UniverseIntegrityReport`,
  `check_universe_integrity()` (non-raising, for tests/tools) and
  `assert_universe_integrity()` (fail-closed).
- `src/psygridevents/universe.py::load_instruments()` — now enforces
  `assert_universe_integrity()` on every load.
- `tools/sync_universe_from_psygrid.py` — the **only** thing that writes
  `config/instruments.json`. Reads Psygrid's `stocks.json` from a local
  checkout (`--psygrid-path`) or a raw URL (`--psygrid-url`), validates the
  canonical contract, records the source commit, and writes the vendored
  file with a diff summary (added/removed symbols vs. the previous version).
- `tools/verify_universe_against_psygrid.py` — read-only. Loads the vendored
  universe, loads Psygrid's actual current `stocks.json`, and asserts exact
  set equality, printing a PASS/FAIL report. Exits non-zero (and prints
  `CANONICAL_UNIVERSE_UNAVAILABLE`) if the reference cannot be reached or the
  sets differ.

## Integrity checks performed

`UniverseIntegrityReport` (from `check_universe_integrity`) surfaces, all in
one deterministic pass:

- `count_matches_expected` — exact count is 990, not "close enough".
- `duplicates` — any symbol appearing more than once.
- `malformed` — any symbol that is empty, has leading/trailing whitespace,
  or is not already upper-case.
- `missing_vs_reference` / `extra_vs_reference` / `set_equal_to_reference`
  — only populated when a reference set is supplied (used by
  `verify_universe_against_psygrid.py` against the live source, and by
  `tests/test_universe_integration.py` against a frozen fixture snapshot of
  Psygrid's `stocks.json` for deterministic, network-free CI).

Any failure makes `is_valid = False`, and `assert_universe_integrity()`
raises with every failure reason concatenated into one message.

## How this was actually verified (not just asserted)

1. `zahidshaikmohammed-cmyk/Psygrid` was cloned and its `stocks.json`
   inspected directly: `universe: "PSYGRID_990"`, 990 symbols, 0 duplicates,
   0 malformed entries, commit `484e482fc71a2a9e0f122f06d783925acb7c98d7`.
2. `tools/sync_universe_from_psygrid.py --psygrid-path <clone>` was run for
   real against that checkout, replacing psygridevents' 450-symbol universe
   with the exact 990 Psygrid symbols and recording the source commit.
3. `tools/verify_universe_against_psygrid.py --psygrid-path <clone>` was run
   for real afterwards and printed `SET EQUAL TO CANONICAL: True` /
   `RESULT: PASS`.
4. The full pytest suite (126 tests, including
   `tests/test_universe_integration.py`) was run against the updated
   universe and passes.
5. `python -m psygridevents.main --once` (with acquisition/market-data
   stubbed, since this environment has no route to live feeds) now reports
   `990 instruments configured`, and a synthetic event for `ACGL` — a symbol
   that exists only in the 990-universe, not the historical 450 — resolved
   through entity resolution, CP8 asset/mechanism mapping, and produced a
   real `EARLY_LONG` CP11 signal, proving resolution actually operates
   against the full 990, not just the pre-existing 450 subset.

## Re-syncing later

When Psygrid's universe changes again, re-run:

```bash
python tools/sync_universe_from_psygrid.py --psygrid-path /path/to/Psygrid
python tools/verify_universe_against_psygrid.py --psygrid-path /path/to/Psygrid
pytest
```

and refresh `tests/fixtures/psygrid_stocks_reference.json` from the same
`stocks.json` so the offline test suite's reference snapshot does not drift
from what was actually synced.

## Explicitly out of scope here

Per the universe-integration mandate, this work stops at the universe
(symbols) layer. It does **not** wire a live market-data adapter — CP10's
`MarketDataAdapter` interface and `NullMarketDataAdapter` default are
unchanged. `Instrument.security_id` resolution (Dhan-specific, live, RAM
only) belongs to that future adapter work, not to universe sync.
