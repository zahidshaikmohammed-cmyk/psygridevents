# Offline Replay — engineering validation only

## Why this exists

The market is not always open, and Psygrid's live feed only has candles
during an active NSE session (see `docs/LIVE_MARKET_DATA_INTEGRATION.md`).
CP10/CP11 still need to be exercised deterministically at any time. The
replay harness (`src/psygridevents/replay.py`) does that: it runs a fixed
set of already-acquired events and a fixed historical market-observation
series through the full CP0–CP11 chain at a sequence of `as_of`
checkpoints.

**This is for engineering validation only.** A signal produced by replay is
a replay artifact, not a claim that a live signal occurred. Nothing in
`main.py`'s `--once`/`--watch`/`--health` paths calls `replay()` — it is
never in the production signal path.

## What it guarantees

At each checkpoint, `replay()` builds a fresh `StaticMarketDataAdapter` call
scoped to that `as_of`, so:

- an observation timestamped after `as_of` is never visible to that
  checkpoint (enforced by `StaticMarketDataAdapter`, and re-checked inside
  CP10's `MarketResponseEngine`);
- a later checkpoint can see strictly more of the same fixed history than
  an earlier one, never less and never something different;
- the same inputs always produce the same output (no wall-clock, no
  randomness, no network).

`tests/test_replay.py::test_no_future_observation_is_ever_visible_to_an_earlier_checkpoint`
is the literal mission example: an event at 10:01, checked at 10:02, must
never see a 10:05 candle. It asserts the checkpoint's
`latest_observation_timestamp_used` never exceeds `as_of`.

## Running it

```bash
python tools/run_replay_demo.py
```

Runs a fixed synthetic RELIANCE order event through three checkpoints
(before any reaction, mid-reaction, and later), printing how many market
observations were visible at each step and the resulting signal state. No
network, no live adapter, safe to run at any time.

For a custom scenario, call `psygridevents.replay.replay()` directly:

```python
from psygridevents.replay import replay
from psygridevents.story_engine import StoryEngine

engine = StoryEngine("config/instruments.json")
checkpoints = replay(engine, observations, market_observations, as_of_list)
for c in checkpoints:
    print(c.as_of, c.signal_states, c.market_observations_used)
```
