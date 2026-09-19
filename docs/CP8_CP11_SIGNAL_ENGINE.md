# CP8–CP11 — Event-Driven Signal Engine

## Purpose

CP0–CP7 answer "what happened, and how important is it". CP8–CP11 answer the
harder question this engine exists for: **is there currently a sufficiently
supported, still-early market opportunity implied by this event, or has it
already run its course?**

This layer never replaces CP0–CP7. It consumes their output (semantic
events, materiality, novelty, transmission/exposure links, CP5 market
confirmation) and adds exactly the missing pieces: an explicit
event→asset→mechanism edge, event freshness, live market-response staging,
exhaustion, and a final signal state.

## CP8 — Event → Asset → Mechanism (`asset_mechanism.py`, `direction.py`)

`AssetMechanismEngine.assess(event)` returns a tuple of `AssetMechanismMapping`
records (an event can imply more than one asset, e.g. a directly named
issuer plus a configured sector exposure). It is a thin, fail-closed
composition of two things that already existed and are otherwise untouched:

- `TransmissionEngine`/`ExposureGraph` (CP2–CP3) for the exposure link itself.
- The new `DirectionEngine`, which classifies documented effect/trigger
  polarity from the same kind of evidence CP5 already used, refactored into
  one shared implementation (`config/direction_rules.yaml`) so "expected
  direction" is never independently re-derived (or re-fabricated) by
  different callers.

One genuine gap is fixed here: the existing exposure graph can only produce
a sector link by chaining through an instrument that was already resolved in
the text (`instrument -> issuer sector -> channel`). A purely macro event
with no named company (an RBI repo-rate decision, a budget announcement)
therefore produced **no exposure at all**. `config/macro_exposure_rules.yaml`
adds an explicit, event-type-scoped macro/sector fallback used *only* when no
instrument was resolved. It never picks an individual stock; the "asset"
values are sector/index labels, not entries in (or additions to) the
canonical 450-instrument universe in `config/instruments.json`, and they
carry reduced confidence because they are not anchored to a specific issuer.

An unresolved issuer, a negated/non-asserted event, or an event type with no
configured channel produces a mapping with `resolved=False` — never a guess.

`AssetMechanismEngine.primary(mappings)` picks the single best-supported
mapping (direct instrument exposure beats sector/index exposure) for the
signal engine, while the full tuple remains available for provenance.

### Wiring verified issuer aliases into entity resolution

`InstrumentResolver.from_issuer_records` already existed but was never
called from `StoryEngine`. `StoryEngine.__init__` now accepts an optional
`issuer_records` tuple; when supplied (from `IssuerMasterBuilder` merging a
verified NSE source file), the resolver is extended with verified company
names only — exactly the existing fail-closed contract in
`tests/test_issuer_truth.py`. `config/issuer_aliases.yaml` remains
intentionally empty until an operator ingests an official NSE source file;
this is why a fresh run resolves `instruments=[]` on many stories. That is
correct, evidence-gated behavior, not a bug — see "Known limitation" below.

## CP9 — Event Timing (`event_timing.py`)

`EventTimingEngine.assess(event, as_of=...)` classifies pure event
freshness — independent of any price data — into `new / early / developing /
late / exhausted / unknown` using `config/event_timing_rules.yaml`
thresholds against `event.event_time` (already documented in CP0 as
"publication time unless an explicit event date was extracted").

It reuses the existing `NoveltyEngine` classification rather than
reinventing freshness logic: an event whose `novelty_status` is `repeat`
never gets back a fresher-than-`late` state, and `stale_repackaged` is
always `exhausted`, regardless of how recently another outlet repeated it.
A missing event timestamp or a negated/non-asserted event yields `unknown`
rather than a guessed state.

## CP10 — Live Market Response (`market_data.py`, `market_response.py`)

`MarketDataAdapter` is the explicit interface for live/historical
observations. `NullMarketDataAdapter` (used by `main.py` today) always
returns no observations — deliberately, so the pipeline can never fabricate
a market reaction. `StaticMarketDataAdapter` serves a pre-supplied,
already-verified set of observations (tests, replay, or a real feed wired in
out-of-band) and enforces the real-time boundary itself.

`MarketResponseEngine.assess(...)` walks the full observation history
available *as of* a given moment (never using an observation timestamped
after `as_of`), establishes the event-time baseline (the last observation at
or before `event_time`), and stages the *aligned* (correct-direction)
displacement into `no_response / early / developing / late / exhausted`
using `config/market_response_rules.yaml` bands, together with:

- `reversal` — a material retracement from the best displacement seen so far,
  which forces `exhausted` regardless of raw magnitude;
- `volume_state` / `vwap_state` / `velocity_state` — corroborating context,
  each explicitly `"unavailable"` (never fabricated) when the observation
  lacks that field;
- `relative_performance` / `sector_relative_performance` when a benchmark or
  sector return is available.

This is intentionally separate from (and does not modify) `market_confirmation.py`
(CP5), which remains the fixed-window, independent-corroboration engine
(`confirmed`/`contradicted`/`mixed`/`untested`) used for the CONFIRMED/
INVALIDATED gates in CP11. CP10 answers "how far along is the move";
CP5 answers "does at least one independent dimension corroborate it".

## CP9+CP10 composite — Exhaustion (`exhaustion.py`)

`ExhaustionEngine.assess(timing, response)` puts the CP9 timing state and the
CP10 response state on the same `early -> developing -> late -> exhausted`
axis and takes the **more advanced** of the two whenever both are known, so
neither a stale-but-quiet event nor a fresh event with an already-extended
price move can look "early" from only one angle. `response` may be `None`
(no market data at all), in which case the state falls back to pure timing.
This is deliberately not a single arbitrary percentage: age, price-stage,
reversal, volume, VWAP and velocity are all folded into `evidence` for
transparency even though the state itself is the two-input maximum.

## CP11 — Signal Engine (`signal_engine.py`)

`SignalEngine.assess(...)` is the only place a directional signal state is
produced, via an explicit gate sequence:

1. Non-asserted/negated event → `NO_SIGNAL`.
2. Unresolved asset → `NO_SIGNAL`.
3. No supported mechanism → `NO_SIGNAL`.
4. Direction not positive/negative → `NO_SIGNAL`.
5. Materiality unknown → `NO_SIGNAL`.
6. Exhaustion state `exhausted` → `EXHAUSTED` (overrides everything below,
   including an independent confirmation).
7. CP5 `market_confirmation.status == "contradicted"` → `INVALIDATED`.
8. No market response evidence yet → `WATCH`.
9. CP5 `market_confirmation.status == "confirmed"` → `CONFIRMED`.
10. Response stage `early`/`developing` → `EARLY_LONG` / `EARLY_SHORT`.
11. Response stage `late` (not exhausted, not confirmed/contradicted) → `WATCH`.

`signal_strength_or_confidence` is computed from `asset_mapping.confidence`,
the event's materiality score, and market-response corroboration — it never
reads `PriorityAssessment`/`priority_score` at all, so it structurally cannot
collapse into the CP6 importance score (verified by
`tests/test_signal_engine.py::test_signal_strength_is_independent_of_priority_score`
and the signature of `SignalEngine.assess`, which has no priority parameter).

`signal_id` is a deterministic hash of `event_id` + `as_of`, so re-running the
same inputs reproduces the same signal, and re-running at a later `as_of`
produces a new, traceable one.

Every `SignalAssessment` field from the mandate's signal contract
(`signal_id`, `event_id`, `story_id`, `timestamp`, `asset`, `asset_type`,
`event_type`, `event_age`, `event_state`, `expected_direction`,
`signal_state`, `signal_strength_or_confidence`, `materiality`,
`transmission_mechanism`, `market_response`, `price_displacement`,
`relative_performance`, `volume_state`, `vwap_state`, `exhaustion_state`,
`trigger`, `invalidation`, `evidence`, `uncertainty`, `source_references`) is
present verbatim on the dataclass.

## Wiring into the pipeline

`StoryEngine` gained five new `build_*` steps mirroring the existing
CP4–CP6 pattern (`build_asset_mechanism`, `build_event_timing`,
`build_market_response`, `build_exhaustion`, `build_signals`), each storing
its result as a new parallel tuple on `StoryIntelligence`. `delivery.py`
exposes `asset_mechanisms`, `event_timings`, `market_responses`,
`exhaustions` and `signals` on every story, and attaches each ranked event's
`signal` next to its existing `priority` (schema version bumped to `1.1`).
`main.py --once` runs the full chain and prints an ASSET/MECHANISM/DIRECTION/
STATE/SIGNAL/TRIGGER/INVALIDATION block per ranked event when a signal was
computed.

## Known limitation: no live market-data credentials

`main.py` wires `NullMarketDataAdapter` because no credentialed/live market
feed is available in this environment. This is intentional fail-closed
behavior, not a placeholder silently masking a shortcut: every signal
produced by a real `--once` run today can only ever be `NO_SIGNAL` (asset
unresolved, since `config/issuer_aliases.yaml` has not yet been populated
from a verified NSE source) or `WATCH` (asset resolved directly by ticker
mention, but no market response evidence exists). `EARLY_LONG`/`EARLY_SHORT`/
`CONFIRMED`/`INVALIDATED`/`EXHAUSTED` are fully implemented and covered by
`tests/test_signal_pipeline.py` against synthetic-but-realistic observation
data; they require wiring a real `MarketDataAdapter` (and, for stronger
entity resolution, a verified issuer master file) to be observed on live
data.
