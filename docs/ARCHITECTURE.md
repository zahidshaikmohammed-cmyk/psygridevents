# Architecture v1.0

## Processing planes

### Plane A — Discovery
Collect source records from configured news, exchange, regulator, government, company and macro adapters.

### Plane B — Truth normalization
Normalize timestamps, titles, publishers, URLs and identifiers. Preserve the raw source record.

### Plane C — Event intelligence
Extract the event, resolve entities, classify event type, identify exposure paths and construct the causal mechanism.

### Plane D — Context
Compare with prior related events, known expectations, existing narratives and other contemporaneous information.

### Plane E — Market confirmation
Optionally consume synchronized market observations to determine whether price/volume/sector behavior confirms, contradicts or does not yet test the event interpretation.

### Plane F — Prioritization
Produce an explainable priority score and materiality class.

### Plane G — Delivery
Expose ranked intelligence through a versioned JSON contract and deterministic CLI output. HTTP/API/dashboard integrations consume this boundary rather than bypassing the intelligence pipeline.

## Event lifecycle

```text
DISCOVERED
  → NORMALIZED
  → DEDUPLICATED
  → EXTRACTED
  → ENTITY-RESOLVED
  → VERIFIED
  → CONTEXTUALIZED
  → IMPACT-ASSESSED
  → MARKET-CONFIRMED / MARKET-CONTRADICTED / UNTESTED
  → PRIORITIZED
  → PUBLISHED
```

An event can move backward in certainty when a source is corrected or contradicted. The original evidence must remain immutable in storage.

## Priority dimensions

The foundation scorer uses eight explicit dimensions:

- source confidence
- novelty
- surprise
- financial materiality
- exposure
- market relevance
- persistence
- transmission

The weights are configuration and will be versioned when calibrated against historical outcomes. The score is an intelligence-prioritization measure, not a probability of price movement.

## CP7 delivery contract

The delivery layer emits a stable top-level JSON object containing `schema_version`, `generated_at`, `engine`, `stories`, `ranked_events` and `summary`. Ranked entries contain the event and its exact priority assessment, including component scores, coverage, available/missing factors and explanation. Source provenance is retained while raw provider payloads are excluded.

## Future modules

1. Source adapters
2. Semantic event extraction
3. Entity/alias resolution
4. Event clustering and story graphs
5. Company-sector relationship graph
6. Expectation/surprise engine
7. Historical event retrieval
8. Market-confirmation adapter
9. Contradiction investigator
10. Narrative state machine
11. Alerting/API layer
12. Historical evaluation and calibration
