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
Expose ranked intelligence as JSON, CLI output and eventually an API/dashboard.

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
  → PUBLISHED
```

An event can move backward in certainty when a source is corrected or contradicted. The original evidence must remain immutable in storage.

## Priority dimensions

The foundation scorer currently uses eight explicit dimensions:

- source confidence
- novelty
- surprise
- financial materiality
- exposure
- market relevance
- persistence
- transmission

The weights are configuration and will be versioned when calibrated against historical outcomes. The score is an intelligence-prioritization measure, not a probability of price movement.

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
