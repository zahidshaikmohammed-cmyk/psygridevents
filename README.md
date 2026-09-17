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

The engine is intentionally hybrid. Deterministic Python handles ingestion, normalization, validation, deduplication, clustering, entity resolution, scoring and data contracts. An optional reasoning layer can enrich events with semantic interpretation without becoming the source of truth for infrastructure.

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

## Current acquisition-to-story pipeline

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
[Next] semantic event extraction
      ↓
[Next] exposure graph
      ↓
[Next] impact + surprise + contradiction engines
      ↓
[Next] market confirmation
      ↓
Intelligence feed
```

Run the package normally for diagnostics:

```bash
python -m psygridevents.main
```

Run the currently enabled verified first-party feeds once:

```bash
python -m psygridevents.main --once
```

No API keys are stored in the repository. Licensed providers remain disabled until credentials and entitlements are supplied.

## Status

**Phase 1 — Source + story foundation implemented.** Provider research, verified-feed configuration, raw RSS acquisition, normalization, deduplication, story clustering, conservative 450-instrument entity resolution, provenance assessment, tests and one-shot CLI diagnostics are now in the repository.

Next: **semantic event extraction + company/sector relationship graph + exposure intelligence.**
