from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml

from .market_confirmation import MarketObservation

RESPONSE_STATES = ("no_response", "early", "developing", "late", "exhausted", "unknown")


@dataclass(frozen=True)
class MarketResponseAssessment:
    """CP10: live/synchronized market response to an event, staged over time.

    Unlike CP5's MarketConfirmationEngine (a fixed post-event reaction window
    used for independent confirm/contradict corroboration), this engine walks
    the full observation history available *as of* a given moment and answers
    "has the market started reacting, and if so, how far along is that move?"
    It obeys the real-time information boundary: only observations timestamped
    at or before `as_of` are ever considered.
    """

    event_id: str
    asset: str
    as_of: datetime
    expected_direction: str
    alignment: str  # "aligned" | "opposed" | "flat" | "unknown"
    price_displacement: float | None
    peak_aligned_displacement: float | None
    relative_performance: float | None
    sector_relative_performance: float | None
    volume_ratio: float | None
    volume_state: str  # "elevated" | "normal" | "unavailable"
    vwap_state: str  # "aligned" | "opposed" | "unavailable"
    velocity_state: str  # "accelerating" | "decelerating" | "steady" | "unavailable"
    reversal: bool
    response_state: str
    observations_used: int
    baseline_timestamp: datetime | None
    latest_timestamp: datetime | None
    uncertainty: tuple[str, ...]
    reason: str


class MarketResponseEngine:
    def __init__(self, rules_file: str | Path) -> None:
        data = yaml.safe_load(Path(rules_file).read_text(encoding="utf-8")) or {}
        self.min_move = float(data.get("min_move_fraction", 0.002))
        self.early_threshold = float(data.get("early_threshold_fraction", 0.010))
        self.developing_threshold = float(data.get("developing_threshold_fraction", 0.020))
        self.late_threshold = float(data.get("late_threshold_fraction", 0.035))
        self.reversal_retracement = float(data.get("reversal_retracement_fraction", 0.40))
        self.elevated_volume_ratio = float(data.get("elevated_volume_ratio", 1.20))

    def assess(
        self,
        *,
        event_id: str,
        asset: str,
        event_time: datetime | None,
        expected_direction: str,
        observations: Iterable[MarketObservation],
        as_of: datetime | None = None,
    ) -> MarketResponseAssessment:
        current = as_of or datetime.now(timezone.utc)

        if event_time is None:
            return self._unknown(event_id, asset, current, expected_direction, "Event timestamp is unavailable.")
        if expected_direction not in ("positive", "negative"):
            return self._unknown(
                event_id,
                asset,
                current,
                expected_direction,
                "Expected direction is not established; market response alignment cannot be evaluated.",
            )

        # Real-time boundary: never use an observation timestamped after `as_of`.
        relevant = sorted(
            (item for item in observations if item.symbol == asset and item.timestamp <= current),
            key=lambda item: item.timestamp,
        )
        baseline_candidates = [item for item in relevant if item.timestamp <= event_time]
        reaction_candidates = [item for item in relevant if item.timestamp > event_time]

        if not baseline_candidates:
            return self._unknown(
                event_id,
                asset,
                current,
                expected_direction,
                "No synchronized market observation exists at or before the event time.",
            )

        baseline = baseline_candidates[-1]
        if baseline.close <= 0:
            return self._unknown(
                event_id, asset, current, expected_direction, "Baseline close is non-positive."
            )

        if not reaction_candidates:
            return MarketResponseAssessment(
                event_id=event_id,
                asset=asset,
                as_of=current,
                expected_direction=expected_direction,
                alignment="unknown",
                price_displacement=None,
                peak_aligned_displacement=None,
                relative_performance=None,
                sector_relative_performance=None,
                volume_ratio=None,
                volume_state="unavailable",
                vwap_state="unavailable",
                velocity_state="unavailable",
                reversal=False,
                response_state="no_response",
                observations_used=1,
                baseline_timestamp=baseline.timestamp,
                latest_timestamp=None,
                uncertainty=("No market observation exists yet after the event.",),
                reason="Event-time baseline is established but no post-event observation is available yet.",
            )

        multiplier = 1.0 if expected_direction == "positive" else -1.0
        series = [
            (item, (item.close / baseline.close) - 1.0, item.close, item.volume, item.average_volume, item.vwap)
            for item in reaction_candidates
        ]
        aligned_series = [(item, displacement * multiplier) for item, displacement, *_ in series]

        current_obs, current_displacement, *_ = series[-1]
        current_aligned = current_displacement * multiplier
        peak_obs, peak_aligned = max(aligned_series, key=lambda pair: pair[1])

        retracement = 0.0
        if peak_aligned > 0:
            retracement = (peak_aligned - current_aligned) / peak_aligned
        reversal = bool(
            peak_aligned > 0
            and peak_obs.timestamp < current_obs.timestamp
            and (current_aligned <= 0 or retracement >= self.reversal_retracement)
        )

        if current_aligned > self.min_move:
            alignment = "aligned"
        elif current_aligned < -self.min_move:
            alignment = "opposed"
        else:
            alignment = "flat"

        volume_ratio = None
        volume_state = "unavailable"
        if current_obs.average_volume is not None and current_obs.average_volume > 0:
            volume_ratio = round(current_obs.volume / current_obs.average_volume, 3)
            volume_state = "elevated" if volume_ratio >= self.elevated_volume_ratio else "normal"

        vwap_state = "unavailable"
        if current_obs.vwap is not None:
            aligned_to_vwap = (current_obs.close - current_obs.vwap) * multiplier
            vwap_state = "aligned" if aligned_to_vwap >= 0 else "opposed"

        relative_performance = None
        if current_obs.benchmark_return is not None:
            relative_performance = round(current_displacement - current_obs.benchmark_return, 6)
        sector_relative_performance = None
        if current_obs.sector_return is not None:
            sector_relative_performance = round(current_displacement - current_obs.sector_return, 6)

        velocity_state = self._velocity(aligned_series)

        uncertainty: list[str] = []
        if current_obs.average_volume is None:
            uncertainty.append("Average volume baseline is unavailable; volume state cannot be assessed.")
        if current_obs.vwap is None:
            uncertainty.append("VWAP is unavailable for the latest observation.")
        if current_obs.benchmark_return is None:
            uncertainty.append("Benchmark return is unavailable; relative performance cannot be assessed.")
        if current_obs.sector_return is None:
            uncertainty.append("Sector return is unavailable; sector-relative performance cannot be assessed.")

        response_state, reason = self._stage(alignment, current_aligned, reversal)

        return MarketResponseAssessment(
            event_id=event_id,
            asset=asset,
            as_of=current,
            expected_direction=expected_direction,
            alignment=alignment,
            price_displacement=round(current_displacement, 6),
            peak_aligned_displacement=round(peak_aligned, 6),
            relative_performance=relative_performance,
            sector_relative_performance=sector_relative_performance,
            volume_ratio=volume_ratio,
            volume_state=volume_state,
            vwap_state=vwap_state,
            velocity_state=velocity_state,
            reversal=reversal,
            response_state=response_state,
            observations_used=len(baseline_candidates[-1:]) + len(reaction_candidates),
            baseline_timestamp=baseline.timestamp,
            latest_timestamp=current_obs.timestamp,
            uncertainty=tuple(uncertainty),
            reason=reason,
        )

    def _stage(self, alignment: str, current_aligned: float, reversal: bool) -> tuple[str, str]:
        if alignment != "aligned":
            return (
                "no_response",
                "Price has not yet moved materially in the expected direction "
                f"(alignment={alignment}).",
            )
        if reversal:
            return (
                "exhausted",
                "Price has retraced materially from its best aligned displacement; "
                "the initial move already appears to have run its course.",
            )
        if current_aligned < self.early_threshold:
            return "early", f"Aligned price displacement ({current_aligned:.4f}) is in the early response band."
        if current_aligned < self.developing_threshold:
            return "developing", f"Aligned price displacement ({current_aligned:.4f}) is developing."
        if current_aligned < self.late_threshold:
            return "late", f"Aligned price displacement ({current_aligned:.4f}) is late-stage without reversal."
        return (
            "exhausted",
            f"Aligned price displacement ({current_aligned:.4f}) has reached the configured extended-move band.",
        )

    @staticmethod
    def _velocity(aligned_series: list[tuple[MarketObservation, float]]) -> str:
        if len(aligned_series) < 2:
            return "unavailable"
        midpoint = max(1, len(aligned_series) // 2)
        first_half = aligned_series[:midpoint]
        second_half = aligned_series[midpoint:]
        if not second_half:
            return "unavailable"
        first_rate = (first_half[-1][1] - first_half[0][1]) if len(first_half) > 1 else first_half[0][1]
        second_rate = second_half[-1][1] - first_half[-1][1]
        if second_rate > first_rate * 1.10:
            return "accelerating"
        if second_rate < first_rate * 0.90:
            return "decelerating"
        return "steady"

    @staticmethod
    def _unknown(
        event_id: str, asset: str, as_of: datetime, expected_direction: str, reason: str
    ) -> MarketResponseAssessment:
        return MarketResponseAssessment(
            event_id=event_id,
            asset=asset,
            as_of=as_of,
            expected_direction=expected_direction,
            alignment="unknown",
            price_displacement=None,
            peak_aligned_displacement=None,
            relative_performance=None,
            sector_relative_performance=None,
            volume_ratio=None,
            volume_state="unavailable",
            vwap_state="unavailable",
            velocity_state="unavailable",
            reversal=False,
            response_state="unknown",
            observations_used=0,
            baseline_timestamp=None,
            latest_timestamp=None,
            uncertainty=(reason,),
            reason=reason,
        )
