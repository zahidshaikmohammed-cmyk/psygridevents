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

The repository monitors the supplied 450-instrument universe. The canonical symbols live in `config/instruments.json` and are treated as configuration/data, not scattered constants.

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
450-instrument entity resolution
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
Machine-readable intelligence
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

## Running

Run the package normally for diagnostics:

```bash
python -m psygridevents.main
```

Run the currently enabled verified first-party feeds once with human-readable ranking:

```bash
python -m psygridevents.main --once
```

Emit the machine-readable delivery contract:

```bash
python -m psygridevents.main --once --json
```

Limit human-readable ranked output:

```bash
python -m psygridevents.main --once --limit 10
```

No API keys are stored in the repository. Licensed providers remain disabled until credentials and entitlements are supplied.

## Status

**CP7 — Delivery/output contract implemented.** The repository now has the semantic/event foundation, explicit transmission, novelty/materiality/contradiction context, synchronized market confirmation, deterministic eight-factor prioritization, and a versioned production-facing JSON/CLI delivery boundary with regression coverage.

Next: production integrations that consume the CP7 contract, followed by historical evaluation and calibration rather than adding opaque scoring layers.
