# CP8–CP10 Event-Driven Signal Engine

## Pipeline

CP8 maps an asserted semantic event to verified configured instruments and an explicit economic mechanism. CP9 classifies event freshness from source publication time and optional observation/detection time. CP10 consumes real market observations through an adapter boundary and evaluates early response versus exhaustion.

Priority remains independent: CP6 answers how important an event is; CP10 answers whether the event is producing an actionable early market opportunity.

## Market adapter

Set `PSYGRID_MARKET_DATA_URL` to an entitled service that returns either a JSON array or:

```json
{"observations":[
  {"symbol":"RELIANCE","timestamp":"2026-09-18T09:31:00+05:30",
   "open":100,"high":101,"low":99.8,"close":100.5,"volume":150000,
   "vwap":100.1,"average_volume":100000,
   "benchmark_return":0.001,"sector_return":0.002}
]}
```

All numeric market fields are supplied by the adapter. The engine does not synthesize prices, volume, VWAP, benchmark returns, sector returns, probabilities, consensus or surprise.

Without the environment variable, the CLI uses an explicit null adapter and emits WATCH/NO_SIGNAL rather than fabricated market signals.

## Signal states

- NO_SIGNAL: required verified event mapping is absent.
- WATCH: evidence is incomplete or the trigger is not yet satisfied.
- EARLY_LONG / EARLY_SHORT: directional response is developing inside the non-exhausted threshold.
- CONFIRMED: reserved for future corroborated signal policy; CP6 priority is never used as a substitute.
- INVALIDATED: market response materially contradicts the mapped direction.
- EXHAUSTED: event-time repricing has already crossed the configured exhaustion threshold.

## Timing

Timing is fail-closed. A missing publication/evaluation timestamp produces UNKNOWN. The engine never replaces missing event time with current time.

## Live validation

The repository's GitHub integration cannot execute local Python commands itself. CI is therefore required to establish `pytest`, `python -m psygridevents.main --once`, and `--once --json` evidence. Live-data validation additionally requires an entitled market-data endpoint.
