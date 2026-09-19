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

## CP8–CP11 event-driven signal engine

See `docs/CP8_CP11_SIGNAL_ENGINE.md` for the full contract. In summary:
CP8 (`asset_mechanism.py`, `direction.py`) maps a semantic event to the
asset(s)/mechanism it implies; CP9 (`event_timing.py`) classifies pure event
freshness; CP10 (`market_data.py`, `market_response.py`) stages the live
market response under a strict real-time information boundary; the CP9+CP10
composite (`exhaustion.py`) determines whether the move is still early or
already spent; CP11 (`signal_engine.py`) combines all of it, plus the
existing CP5 market confirmation and CP6 materiality, into one explicit
`NO_SIGNAL/WATCH/EARLY_LONG/EARLY_SHORT/CONFIRMED/INVALIDATED/EXHAUSTED`
signal that is deliberately independent of the CP6 priority score.

## Future modules

1. Credentialed live market-data adapter (CP10 currently runs fail-closed with no observations)
2. Verified NSE issuer-master ingestion (unlocks broader CP8 entity resolution)
3. Historical replay/backtest validation of signal timing without future leakage
4. Alerting/API layer
5. Historical evaluation and calibration of the CP6 priority weights
