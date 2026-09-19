#!/usr/bin/env python3
"""Deterministic offline replay demo (engineering validation only).

Runs a fixed, synthetic order event for RELIANCE through the full CP0-CP11
chain at three as_of checkpoints (before any market reaction, mid-reaction,
and after the reaction has developed further), using only
psygridevents.replay.replay() -- no acquisition, no live market-data
adapter, no network. Safe to run at any time, market open or closed.

This output is a replay artifact for engineering validation. It is NOT a
claim that a live signal occurred; see docs/OFFLINE_REPLAY.md.

Usage:
    python tools/run_replay_demo.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from psygridevents.acquisition import RawObservation  # noqa: E402
from psygridevents.market_confirmation import MarketObservation  # noqa: E402
from psygridevents.replay import replay  # noqa: E402
from psygridevents.story_engine import StoryEngine  # noqa: E402

EVENT_TIME = datetime(2026, 9, 21, 10, 1, tzinfo=timezone.utc)  # a Monday


def main() -> int:
    observation = RawObservation(
        provider_id="replay-demo", source_tier=0, publisher="Official Source",
        title="RELIANCE wins order worth INR 500 crore", url="https://example.com/replay-demo",
        summary="RELIANCE wins order worth INR 500 crore",
        published_at=EVENT_TIME, observed_at=EVENT_TIME, raw={},
    )
    market_observations = [
        MarketObservation(symbol="RELIANCE", timestamp=EVENT_TIME - timedelta(minutes=5),
                           open=1000.0, high=1000.0, low=1000.0, close=1000.0, volume=1000.0),
        MarketObservation(symbol="RELIANCE", timestamp=EVENT_TIME + timedelta(minutes=8),
                           open=1005.0, high=1005.0, low=1005.0, close=1005.0, volume=1000.0),
        MarketObservation(symbol="RELIANCE", timestamp=EVENT_TIME + timedelta(minutes=40),
                           open=1006.0, high=1006.0, low=1006.0, close=1006.0, volume=1000.0),
    ]
    checkpoints = [
        EVENT_TIME - timedelta(minutes=1),
        EVENT_TIME + timedelta(minutes=10),
        EVENT_TIME + timedelta(minutes=45),
    ]

    engine = StoryEngine(ROOT / "config" / "instruments.json")
    results = replay(engine, [observation], market_observations, checkpoints)

    print("DETERMINISTIC OFFLINE REPLAY -- ENGINEERING VALIDATION ONLY")
    print("Not a claim that a live signal occurred.\n")
    for checkpoint in results:
        print(f"as_of={checkpoint.as_of.isoformat()}")
        print(f"  market observations visible: {checkpoint.market_observations_used}")
        print(f"  latest observation used: {checkpoint.latest_observation_timestamp_used}")
        for event_id, state in checkpoint.signal_states.items():
            print(f"  signal[{event_id}] = {state}")
        print()

    return 0


if __name__ == "__main__":
    sys.exit(main())
