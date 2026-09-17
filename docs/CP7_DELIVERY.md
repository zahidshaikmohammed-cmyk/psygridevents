# CP7 — Delivery Contract v1.0

## Purpose

CP7 is the production-facing delivery boundary. It converts completed story/event intelligence into a stable, machine-readable payload and a deterministic human-readable ranked CLI view.

## Machine-readable contract

`psygridevents.main --once --json` emits one JSON document with these top-level fields:

- `schema_version`
- `generated_at`
- `engine`
- `stories`
- `ranked_events`
- `summary`

Each `ranked_events` entry contains:

- `story_id`
- `event`
- `priority`

The `priority` object preserves the score, class, model coverage, component scores, available factors, missing factors and reason. This prevents downstream consumers from losing the explanation for a ranking.

## Provenance rules

- Source publisher, provider, tier, title, URL and timestamps are retained.
- Raw provider payloads are not exposed through the delivery contract.
- Datetimes are serialized as ISO-8601 strings.
- Missing intelligence remains explicitly missing.
- No synthetic market observations or expectations are added by delivery.

## CLI contract

`python -m psygridevents.main --once` runs acquisition, materiality, prioritization and deterministic ranking, then prints the ranked intelligence summary.

`--limit N` controls the number of ranked events shown in human-readable output.

`--json` is intended for downstream programs and requires `--once`.

## Scope boundary

CP7 does not create an HTTP server, dashboard, notification service or trading execution interface. Those are separate production integration layers and must consume this contract rather than bypassing the intelligence pipeline.
