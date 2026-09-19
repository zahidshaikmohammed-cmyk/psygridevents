# PSYGRIDEVENTS

## Event & News Intelligence Engine

PSYGRIDEVENTS is a precision-first event intelligence system for a defined universe of Indian financial instruments.

Its job is not to collect headlines and attach a generic sentiment label. It is designed to:

1. Discover relevant news, announcements, macro events and external developments.
2. Verify source quality and event status.
3. Deduplicate the same event across multiple sources.
4. Cluster coverage into evolving stories rather than treating every article as a new event.
5. Extract a structured event from unstructured information.
6. Resolve companies, sectors, commodities, regulators and related entities.
7. Estimate direct, sector, competitive, supply-chain and second-order exposure.
8. Separate facts from interpretation and interpretation from market implication.
9. Measure novelty, surprise, materiality, persistence and time horizon.
10. Detect conflicting narratives and contradictory market reactions.
11. Rank events so that the most important information reaches the top.
12. Produce machine-readable intelligence for downstream trading systems.

## Design principle

**FACT → EVENT → EXPOSURE → MECHANISM → IMPACT → CONTEXT → CONFIRMATION → INTELLIGENCE**

The engine is intentionally hybrid. Deterministic Python handles ingestion, normalization, validation, deduplication, clustering, entity resolution, semantic extraction, exposure mapping, prioritization and delivery contracts. A future reasoning layer can enrich events with semantic interpretation without becoming the source of truth for infrastructure.

## Precision principles

- No headline-only trading decisions.
- No fabricated events, sources, prices or expectations.
- Unknown information remains unknown.
- Source reliability is explicit.
- Event time and publication time are distinct.
- A repeated headline is not a new event.
- A positive headline does not automatically imply positive market impact.
- Expected events and surprise events are treated differently.
- Direct and second-order exposure are separated.
- Contradictions are surfaced rather than hidden.
- Every material conclusion must retain an evidence trail.
- Multiple articles from one publisher do not count as independent corroboration.
- Undocumented or guessed feed URLs are never activated.
- Semantic extraction records what the source states; it does not invent surprise, direction or materiality.
- Hypothetical, planned, reported and negated language is retained explicitly.
- Second-order exposure is fail-closed unless the issuer/sector relationship is explicitly configured.

## Provider architecture

The engine uses multiple source classes instead of trusting one vendor:

```text
TIER 0 — FIRST-PARTY TRUTH
NSE / BSE / SEBI / RBI / PIB / Ministries / Company IR

TIER 1 — PREMIUM FINANCIAL WIRES
LSEG/Reuters / Bloomberg / FactSet / Dow Jones

TIER 2 — EVENT & NEWS ANALYTICS
RavenPack / Perigon / NewsCatcher / Benzinga

TIER 3 — BROAD DISCOVERY / CONTEXT
GDELT / Polygon-Massive / MarketAux / NewsAPI

TIER 4 — OPEN WEB / SOCIAL
Discovery leads only; never confirmation by itself.
```

Provider research and activation policy live in `docs/PROVIDER_RESEARCH.md`, `config/providers.json`, and `config/feed_endpoints.yaml`.

## Initial universe

The repository monitors a 990-instrument universe. `zahidshaikmohammed-cmyk/Psygrid`'s `stocks.json` is the single canonical owner of this universe; `config/instruments.json` is a synced, provenance-stamped mirror of it (never hand-edited), validated on every load against the same strict contract Psygrid enforces on itself (exactly 990 unique, well-formed symbols) and rejected — never silently downgraded — if that contract is not met. See `docs/UNIVERSE_INTEGRATION.md` for the sync/verification mechanism and tools.

## Acquisition → intelligence pipeline

```text
Verified source feed
      ↓
Raw observation
      ↓
Text / timestamp normalization
      ↓
Conservative deduplication
      ↓
Evolving story clustering
      ↓
990-instrument entity resolution
      ↓
Evidence / corroboration assessment
      ↓
Canonical event taxonomy
      ↓
Semantic event extraction
      ↓
Explicit exposure graph
      ↓
Novelty + materiality + contradiction context
      ↓
Synchronized market confirmation
      ↓
CP6 explainable prioritization
      ↓
Deterministic ranking
      ↓
CP7 delivery contract
      ↓
CP8 event → asset → mechanism
      ↓
CP9 event timing / freshness state
      ↓
CP10 live market response (real-time boundary)
      ↓
CP9+CP10 exhaustion state
      ↓
CP11 event-driven signal (NO_SIGNAL/WATCH/EARLY_LONG/EARLY_SHORT/CONFIRMED/INVALIDATED/EXHAUSTED)
      ↓
Machine-readable intelligence + signal
```

### Semantic event contract

Every extracted event is structured around:

```text
WHAT HAPPENED
WHO / INSTRUMENTS
EVENT TYPE
TRIGGER
MAGNITUDE
DIRECT EFFECT
INDIRECT EFFECT
COMPETITOR EFFECT
SUPPLY-CHAIN EFFECT
TIME HORIZON
MODALITY / NEGATION
NOVELTY STATUS
SURPRISE STATUS
EVIDENCE
UNCERTAINTY
MARKET MECHANISM
```

The semantic layer does not invent surprise, direction or materiality. Dedicated engines attach those assessments when the required evidence exists.

### Exposure contract

Direct exposure can be emitted when an event explicitly resolves to a configured instrument. Sector and second-order links require an explicit issuer/sector relationship graph. Generic intuition is not converted into a graph edge automatically.

### CP6 prioritization contract

Priority uses eight configured dimensions: source confidence, novelty, surprise, financial materiality, exposure, market relevance, persistence and transmission. Missing or explicitly unknown factors are excluded rather than fabricated, and model coverage is retained with every assessment. The score is an explainable prioritization measure, not a probability of price movement.

### CP7 delivery contract

`python -m psygridevents.main --once --json` emits a versioned JSON document containing story provenance, structured events and ranked events. Every ranked event carries its event payload and the exact priority assessment, including component scores, coverage and missing factors. Raw provider payloads are excluded from the delivery boundary. See `docs/CP7_DELIVERY.md` for the full contract.

### CP8–CP11 event-driven signal engine

The engine does not stop at ranking importance. For every semantic event it also asks: which configured asset (instrument, sector, or index) does this imply, through what documented mechanism, in what expected direction (CP8); how fresh is the event and is a repeated/repackaged story ever treated as a new opportunity (CP9); has the market actually started reacting, obeying a strict real-time information boundary (CP10); and is that reaction still early/developing or already exhausted (CP9+CP10)? CP11 combines all of this — plus the existing CP5 market-confirmation and CP6 materiality — into one explicit `NO_SIGNAL / WATCH / EARLY_LONG / EARLY_SHORT / CONFIRMED / INVALIDATED / EXHAUSTED` signal, independent of the CP6 priority score, with an explicit trigger, invalidation condition, evidence and uncertainty. See `docs/CP8_CP11_SIGNAL_ENGINE.md` for the full contract.

### Live market data

CP10's live market observations are consumed from `zahidshaikmohammed-cmyk/Psygrid`'s own public JSON contract (`/public/stock/{symbol}.json`, `/public/live.json`) rather than a second, independent Dhan integration — Psygrid already owns Dhan authentication, security-ID resolution, WebSocket ingestion and reconnect handling. `PsygridMarketDataAdapter` is the production default; `NullMarketDataAdapter` (never fabricates an observation) remains available for offline/CI use via `--market-data none`. See `docs/LIVE_MARKET_DATA_INTEGRATION.md` for the full design, freshness/real-time-boundary rules, and the current status of live validation against Oracle.

## Running

Run the package normally for diagnostics:

```bash
python -m psygridevents.main
```

Run the currently enabled verified first-party feeds once with human-readable ranking, consuming Psygrid's live market data by default:

```bash
python -m psygridevents.main --once
```

Run without live market data (fail-closed, offline/CI):

```bash
python -m psygridevents.main --once --market-data none
```

Emit the machine-readable delivery contract:

```bash
python -m psygridevents.main --once --json
```

Limit human-readable ranked output:

```bash
python -m psygridevents.main --once --limit 10
```

Run continuously, polling for new events/market state and printing only signal changes (safe to restart -- see `docs/OPERATIONS.md`):

```bash
python -m psygridevents.main --watch --interval 60
```

Print a diagnostic health/status snapshot (never runs acquisition, never affects a signal):

```bash
python -m psygridevents.main --health
```

Run the deterministic offline replay harness (engineering validation only; safe with the market closed):

```bash
python tools/run_replay_demo.py
```

No API keys are stored in the repository. Licensed providers remain disabled until credentials and entitlements are supplied. See `docs/OPERATIONS.md` for the full CLI reference, the `MARKET_OPEN`/`MARKET_CLOSED`/`MARKET_DATA_UNAVAILABLE` distinction, health/status fields, and the persistence/idempotency contract.

## Status

**CP11 — Event-driven signal engine implemented, wired to live Psygrid market data, and hardened for production operation while the market is closed.** The repository has the semantic/event foundation, explicit transmission, novelty/materiality/contradiction context, synchronized market confirmation, deterministic eight-factor prioritization, a versioned production-facing JSON/CLI delivery boundary, the CP8–CP11 event → asset → mechanism → timing → live market response → exhaustion → signal chain, a 990-instrument universe synced from and verified against Psygrid's canonical source, a production `MarketDataAdapter` that consumes Psygrid's live feed, per-provider acquisition failure isolation, restart-safe publication persistence, a diagnostic health/status surface, and a deterministic offline replay harness — all with regression coverage (220+ tests).

Everything except live signal generation has been verified while the market is closed: the complete offline pipeline, the 990-universe equality check (re-verified live against Psygrid's current GitHub source), the Psygrid adapter's fail-closed/timeout/malformed-data/market-state handling, and deterministic replay proving no future observation ever leaks into an earlier checkpoint. This session has no network route to the Oracle production host itself, so live signal content (an actual `EARLY_LONG`/`CONFIRMED` from real ticks) has not been observed — that is the one thing intentionally left for the next live NSE session. See `docs/LIVE_MARKET_DATA_INTEGRATION.md` and `docs/OPERATIONS.md`.

Next: wire a credentialed live market-data adapter and a verified NSE issuer-master source, then run historical replay validation (CP: timing/leakage evaluation) rather than adding opaque scoring layers.
