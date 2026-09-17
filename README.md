# PSYGRIDEVENTS

## Event & News Intelligence Engine

PSYGRIDEVENTS is a precision-first event intelligence system for a defined universe of Indian financial instruments.

Its job is not to collect headlines and attach a generic sentiment label. It is designed to:

1. Discover relevant news, announcements, macro events and external developments.
2. Verify source quality and event status.
3. Deduplicate the same event across multiple sources.
4. Extract a structured event from unstructured information.
5. Resolve companies, sectors, commodities, regulators and related entities.
6. Estimate direct, sector, competitive, supply-chain and second-order exposure.
7. Separate facts from interpretation and interpretation from market implication.
8. Measure novelty, surprise, materiality, persistence and time horizon.
9. Detect conflicting narratives and contradictory market reactions.
10. Rank events so that the most important information reaches the top.
11. Produce machine-readable intelligence for downstream trading systems.

## Design principle

**FACT → EVENT → EXPOSURE → MECHANISM → IMPACT → CONTEXT → CONFIRMATION → INTELLIGENCE**

The engine is intentionally hybrid. Deterministic Python handles ingestion, normalization, validation, deduplication, scoring and data contracts. An optional reasoning layer can enrich events with semantic interpretation without becoming the source of truth for infrastructure.

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

## Initial universe

The repository is intended to monitor the supplied 450-instrument universe. The canonical symbols belong in `config/instruments.json` and should be treated as configuration/data, not hard-coded throughout the engine.

## Architecture

```text
Sources
  ↓
Ingestion
  ↓
Normalization
  ↓
Deduplication
  ↓
Event Extraction
  ↓
Entity Resolution
  ↓
Exposure Mapping
  ↓
Materiality / Impact
  ↓
Context / Surprise / Contradiction
  ↓
Market Confirmation
  ↓
Priority Ranking
  ↓
Intelligence Feed / API
```

## Status

**Phase 0 — Foundation implementation.** The core contracts, scoring primitives and pipeline skeleton are being established before live source adapters are added.

This repository must evolve through tests and explicit versioned contracts rather than ad-hoc changes.