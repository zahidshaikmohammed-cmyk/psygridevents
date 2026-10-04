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

## Production service (v1.0)

The planes above are implemented end to end by the production service. See
[PRODUCTION.md](PRODUCTION.md) for the module map:

* **Discovery:** `providers/`
* **Normalization and fusion:** `fusion.py`
* **Event intelligence:** `event_pipeline.py`, `event_classifier.py`, `entity_resolution.EntityResolver`, `exposure_graph.py`
* **Market confirmation:** `market_snapshot.py`, `reaction.py`
* **Prioritization:** `opportunity.py`, `signal_book.py`
* **Delivery:** `api.py`, `alerts.py`
* **Persistence:** `storage.py`

It reuses the CP2–CP11 engines: semantic extraction, direction, materiality, novelty,
contradiction, event timing, exhaustion and the CP11 `SignalEngine`.

## Future modules

1. Calibration of `opportunity_score` weights against the accumulating outcome memory (no probability is claimed until then)
2. PDF parsing of exchange filings whose substance is only in the attachment
3. Documented supplier/customer edges for the exposure graph
4. Historical replay of stored events against stored outcomes
