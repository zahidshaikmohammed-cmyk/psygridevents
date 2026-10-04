"""Historical memory: post-signal / post-event outcome measurement from real PSYGRID candles.

For every signal that becomes actionable (and every direct, material event
first observed during the session, to measure missed signals) the engine
records the reference price and then measures directional returns at
+1/5/15/30/60 minutes, maximum favourable/adverse excursion, time to peak,
time to invalidation and whether the move became exhausted. These rows are
the raw material for future statistical learning; no probability is claimed
until enough clean observations exist (see Store.outcome_statistics).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from .market_snapshot import MarketSnapshot
from .storage import Store

HORIZONS_MINUTES = (1, 5, 15, 30, 60)


class OutcomeTracker:
    def __init__(self, store: Store) -> None:
        self.store = store
        self.open: dict[str, dict[str, Any]] = {}

    def restore(self) -> int:
        self.open = {item["outcome_id"]: item for item in self.store.outcomes(completed=False, limit=5000)}
        return len(self.open)

    def start(
        self,
        *,
        outcome_id: str,
        signal_key: str,
        event_id: str,
        symbol: str,
        event_type: str,
        direction: str,
        reference_kind: str,
        reference_at: datetime,
        reference_price: float,
        state: str,
        score: float | None,
        actionable: bool,
        market_state: dict[str, Any],
        invalidation_price: float | None,
        trade_date: str,
    ) -> bool:
        if outcome_id in self.open or self.store.outcome_exists(outcome_id):
            return False
        if direction not in ("positive", "negative") or reference_price <= 0:
            return False
        record = {
            "outcome_id": outcome_id, "signal_key": signal_key, "event_id": event_id, "symbol": symbol,
            "event_type": event_type, "direction": direction, "reference_kind": reference_kind,
            "reference_at": reference_at, "reference_price": reference_price, "state_at_reference": state,
            "opportunity_score_at_reference": score, "actionable": actionable,
            "market_state": market_state | {"invalidation_price": invalidation_price}, "returns": {}, "mfe": None,
            "mae": None, "time_to_peak_minutes": None, "time_to_invalidation_minutes": None,
            "became_exhausted": False, "final_state": state, "completed": False, "trade_date": trade_date,
        }
        self.open[outcome_id] = record
        self.store.upsert_outcome(record)
        return True

    def note_state(self, signal_key: str, state: str) -> None:
        for record in self.open.values():
            if record["signal_key"] == signal_key:
                record["final_state"] = state
                if state == "EXHAUSTED":
                    record["became_exhausted"] = True

    def update(self, snapshot: MarketSnapshot, *, now: datetime, session_closed: bool) -> int:
        completed = 0
        for outcome_id, record in list(self.open.items()):
            series = snapshot.series.get(record["symbol"])
            if series is None or not len(series):
                if session_closed:
                    record["completed"] = True
                    record["market_state"]["completion_note"] = "no market data after reference"
                    self.store.upsert_outcome(record)
                    del self.open[outcome_id]
                    completed += 1
                continue
            sign = 1.0 if record["direction"] == "positive" else -1.0
            reference = float(record["reference_price"])
            reference_at: datetime = record["reference_at"]
            ref_epoch = reference_at.timestamp()
            returns: dict[str, float] = dict(record.get("returns") or {})
            for minutes in HORIZONS_MINUTES:
                target = ref_epoch + minutes * 60
                # the bar that *completes* at or before the horizon (bar start + 60s <= target)
                index = series.index_at_or_before(target - 60)
                if index >= 0 and series.ts[index] >= ref_epoch - 60 and series.ts[index] + 60 <= now.timestamp():
                    if target <= now.timestamp():
                        returns[f"{minutes}m"] = round((series.close[index] / reference - 1.0) * sign, 6)
            window_end = ref_epoch + 60 * 60
            best = worst = 0.0
            peak_minutes = None
            invalidated_minutes = record.get("time_to_invalidation_minutes")
            invalidation_price = record["market_state"].get("invalidation_price")
            for index in range(len(series)):
                start = series.ts[index]
                if start < ref_epoch - 60 or start > window_end:
                    continue
                favourable = ((series.high[index] if sign > 0 else series.low[index]) / reference - 1.0) * sign
                adverse = ((series.low[index] if sign > 0 else series.high[index]) / reference - 1.0) * sign
                if favourable > best:
                    best = favourable
                    peak_minutes = round((start + 60 - ref_epoch) / 60.0, 1)
                worst = min(worst, adverse)
                if invalidated_minutes is None and invalidation_price:
                    crossed = series.low[index] <= invalidation_price if sign > 0 else series.high[index] >= invalidation_price
                    if crossed:
                        invalidated_minutes = round((start + 60 - ref_epoch) / 60.0, 1)
            record["returns"] = returns
            record["mfe"] = round(best, 6)
            record["mae"] = round(worst, 6)
            record["time_to_peak_minutes"] = peak_minutes
            record["time_to_invalidation_minutes"] = invalidated_minutes
            done = "60m" in returns or session_closed or now - reference_at > timedelta(hours=8)
            if done:
                record["completed"] = True
                if "60m" not in returns:
                    record["market_state"]["completion_note"] = "session ended before the 60m horizon"
                del self.open[outcome_id]
                completed += 1
            self.store.upsert_outcome(record)
        return completed
