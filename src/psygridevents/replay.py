from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .acquisition import RawObservation
from .market_confirmation import MarketObservation
from .market_data import StaticMarketDataAdapter
from .story_engine import StoryEngine, StoryIntelligence

# Because the market is closed, offline replay is the only way to exercise
# CP10/CP11 against something other than an empty live response. This module
# is for engineering validation ONLY: a signal produced here is a replay
# artifact, never a claim that a live signal occurred (see docs/OFFLINE_REPLAY.md).


@dataclass(frozen=True)
class ReplayCheckpoint:
    as_of: datetime
    stories: tuple[StoryIntelligence, ...]
    signal_states: dict[str, str]
    market_observations_used: int
    latest_observation_timestamp_used: datetime | None


def replay(
    engine: StoryEngine,
    observations: list[RawObservation],
    market_observations: list[MarketObservation],
    as_of_checkpoints: list[datetime],
) -> list[ReplayCheckpoint]:
    """Walk a fixed set of already-acquired observations forward through
    a sequence of `as_of` checkpoints, running the full CP0-CP11 chain at
    each one.

    `market_observations` is the complete historical series; a
    `StaticMarketDataAdapter` enforces that only entries with
    `timestamp <= as_of` are ever visible for a given checkpoint -- a
    checkpoint at 10:02 can never see a 10:05 candle, no matter what is in
    the full series or what a later checkpoint will see.

    This never touches acquisition or a live market-data adapter: it is a
    deterministic function of its inputs, safe to run with the market closed.
    """
    market_data = StaticMarketDataAdapter(market_observations)
    checkpoints: list[ReplayCheckpoint] = []

    for as_of in sorted(as_of_checkpoints):
        stories = engine.build_stories(observations)
        stories = engine.build_materiality(stories)
        stories = engine.build_asset_mechanism(stories)

        market_obs_used: list[MarketObservation] = []
        for story in stories:
            for mappings in story.asset_mechanisms:
                for mapping in mappings:
                    if mapping.resolved and mapping.asset:
                        market_obs_used.extend(market_data.observations(mapping.asset, as_of=as_of))

        stories = engine.build_market_confirmation(stories, market_obs_used)
        stories = engine.build_event_timing(stories, as_of=as_of)
        stories = engine.build_market_response(stories, market_obs_used, as_of=as_of)
        stories = engine.build_exhaustion(stories)
        stories = engine.build_signals(stories, as_of=as_of)

        signal_states = {signal.event_id: signal.signal_state for item in stories for signal in item.signals}
        latest_ts = max((o.timestamp for o in market_obs_used), default=None)
        checkpoints.append(
            ReplayCheckpoint(
                as_of=as_of,
                stories=tuple(stories),
                signal_states=signal_states,
                market_observations_used=len(market_obs_used),
                latest_observation_timestamp_used=latest_ts,
            )
        )

    return checkpoints
