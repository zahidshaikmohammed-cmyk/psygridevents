# CP6 — Explainable Intelligence Prioritization

## Purpose

CP6 converts the event-intelligence context into a deterministic, auditable priority score for ordering information. It is a prioritization measure, not a probability of price movement and not a trading signal.

## Factors

The configured model uses eight factors:

1. source confidence
2. novelty
3. surprise
4. financial materiality
5. exposure
6. market relevance
7. persistence
8. transmission

Weights live in `config/priority_rules.yaml` and are versioned configuration.

## Missing-data policy

An unavailable or explicitly unknown factor is **not** fabricated as zero. It is excluded from the weighted mean and listed in `missing_factors`.

`coverage` records the fraction of configured model weight represented by observed factors. This prevents a sparse record from pretending to contain a complete assessment.

High and critical classes also require minimum configured coverage.

## Factor sourcing

- Source confidence comes from the best available evidence tier.
- Novelty comes only from the dedicated novelty assessment.
- Surprise is scored only for explicit assessed states (`surprising_high`, `surprising_low`, `in_line`).
- Financial materiality comes only from the materiality engine.
- Exposure and transmission come from explicit transmission links.
- Market relevance is a configured baseline by canonical event type; it does not predict direction.
- Persistence comes from an explicitly assessed time horizon; `unknown` remains unscored.

Contradiction and market-confirmation states remain separate fields. CP6 does not silently invent penalties or bonuses for them.

## Ranking

Ranking is deterministic:

```text
priority_score ↓
coverage ↓
event_id ↓
```

The component scores, available factors, missing factors and reason are retained with each assessment so the ordering can be audited.

## Pipeline integration

`StoryEngine.build_prioritization()` attaches CP6 assessments to each `StoryIntelligence`. `StoryEngine.rank_prioritization()` returns all attached assessments in deterministic descending order.
