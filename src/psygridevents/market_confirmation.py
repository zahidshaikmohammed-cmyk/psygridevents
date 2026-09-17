from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import re
import yaml

from .semantic import SemanticEvent


@dataclass(frozen=True)
class MarketObservation:
    """One synchronized OHLCV observation for a configured instrument."""

    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None
    average_volume: float | None = None
    benchmark_return: float | None = None
    sector_return: float | None = None


@dataclass(frozen=True)
class MarketConfirmationAssessment:
    event_id: str
    status: str
    expected_direction: str
    observed_return: float | None
    relative_return: float | None
    volume_ratio: float | None
    vwap_relation: str
    sector_alignment: str
    observations_used: int
    window_start: datetime | None
    window_end: datetime | None
    score: float
    reason: str


class MarketConfirmationEngine:
    """Compare an asserted event with synchronized post-event market behavior."""

    def __init__(self, rules_file: str | Path) -> None:
        data = yaml.safe_load(Path(rules_file).read_text(encoding="utf-8")) or {}
        self.min_move = float(data.get("min_move_fraction", 0.002))
        self.min_volume_ratio = float(data.get("min_volume_ratio", 1.20))
        self.min_reaction_minutes = int(data.get("min_reaction_minutes", 5))
        self.max_reaction_minutes = int(data.get("max_reaction_minutes", 30))
        self.confirm_threshold = float(data.get("confirm_threshold", 0.65))
        self.contradict_threshold = float(data.get("contradict_threshold", 0.35))
        self.positive_patterns = tuple(data.get("positive_patterns", []))
        self.negative_patterns = tuple(data.get("negative_patterns", []))

    @staticmethod
    def _polarity(event: SemanticEvent, positive_patterns: tuple[str, ...], negative_patterns: tuple[str, ...]) -> str:
        def classify(text: str) -> str:
            positive = any(re.search(pattern, text, re.IGNORECASE) for pattern in positive_patterns)
            negative = any(re.search(pattern, text, re.IGNORECASE) for pattern in negative_patterns)
            if positive and not negative:
                return "positive"
            if negative and not positive:
                return "negative"
            return "neutral"

        # Effects carry more directional meaning than generic trigger words.
        effect_text = " ".join(
            value or ""
            for value in (
                event.direct_effect,
                event.indirect_effect,
                event.competitor_effect,
                event.supply_chain_effect,
            )
        )
        effect_polarity = classify(effect_text)
        if effect_polarity != "neutral":
            return effect_polarity
        trigger_text = " ".join((event.trigger or "",))
        return classify(trigger_text)

    @staticmethod
    def _sign_matches(value: float, expected: str, threshold: float = 0.0) -> bool:
        return value >= threshold if expected == "positive" else value <= -threshold

    @staticmethod
    def _alignment(value: float | None, expected: str, threshold: float = 0.0) -> str:
        if value is None:
            return "not_available"
        if expected == "positive":
            if value > threshold:
                return "aligned"
            if value < -threshold:
                return "opposed"
        elif expected == "negative":
            if value < -threshold:
                return "aligned"
            if value > threshold:
                return "opposed"
        return "neutral"

    def assess(self, event: SemanticEvent, observations: Iterable[MarketObservation]) -> MarketConfirmationAssessment:
        expected = self._polarity(event, self.positive_patterns, self.negative_patterns)
        if event.negated or event.modality != "asserted":
            return self._untested(event, expected, "Non-asserted or negated event cannot be market-confirmed.")
        if not event.instruments or event.event_time is None:
            return self._untested(event, expected, "Instrument or event timestamp is missing.")
        if expected == "neutral":
            return self._untested(event, expected, "No unambiguous documented event polarity is available for market testing.")

        relevant = sorted((item for item in observations if item.symbol in event.instruments), key=lambda item: item.timestamp)
        if not relevant:
            return self._untested(event, expected, "No synchronized market observations were supplied for the event instrument.")

        event_time = event.event_time
        earliest = event_time + timedelta(minutes=self.min_reaction_minutes)
        latest = event_time + timedelta(minutes=self.max_reaction_minutes)
        baseline_candidates = [item for item in relevant if item.timestamp <= event_time]
        reaction_candidates = [item for item in relevant if earliest <= item.timestamp <= latest]
        if not baseline_candidates or not reaction_candidates:
            return self._untested(event, expected, "Synchronized baseline and post-event reaction observations are required before confirmation.")

        baseline = baseline_candidates[-1]
        terminal = reaction_candidates[-1]
        if baseline.close <= 0:
            return self._untested(event, expected, "Baseline close is non-positive and cannot support a return calculation.")

        observed_return = (terminal.close / baseline.close) - 1.0
        relative_return = None if terminal.benchmark_return is None else observed_return - terminal.benchmark_return
        volume_ratio = None
        if terminal.average_volume is not None and terminal.average_volume > 0:
            volume_ratio = terminal.volume / terminal.average_volume

        vwap_relation = "not_available"
        if terminal.vwap is not None:
            vwap_relation = "aligned" if self._sign_matches(terminal.close - terminal.vwap, expected) else "opposed"

        sector_alignment = self._alignment(terminal.sector_return, expected, self.min_move)
        relative_alignment = self._alignment(relative_return, expected, self.min_move)
        price_aligned = self._sign_matches(observed_return, expected, self.min_move)
        opposite = "negative" if expected == "positive" else "positive"
        price_opposed = self._sign_matches(observed_return, opposite, self.min_move)

        corroborators = 0
        available_dimensions = 1
        if volume_ratio is not None:
            available_dimensions += 1
            corroborators += int(volume_ratio >= self.min_volume_ratio)
        if terminal.vwap is not None:
            available_dimensions += 1
            corroborators += int(vwap_relation == "aligned")
        if relative_return is not None:
            available_dimensions += 1
            corroborators += int(relative_alignment == "aligned")
        if terminal.sector_return is not None:
            available_dimensions += 1
            corroborators += int(sector_alignment == "aligned")

        score = (1.0 if price_aligned else 0.0 if price_opposed else 0.5) / available_dimensions
        if volume_ratio is not None:
            score += (1.0 if volume_ratio >= self.min_volume_ratio else 0.0) / available_dimensions
        if terminal.vwap is not None:
            score += (1.0 if vwap_relation == "aligned" else 0.0) / available_dimensions
        if relative_return is not None:
            score += (1.0 if relative_alignment == "aligned" else 0.0) / available_dimensions
        if terminal.sector_return is not None:
            score += (1.0 if sector_alignment == "aligned" else 0.0) / available_dimensions
        score = round(score, 3)

        if price_opposed:
            status = "contradicted"
            reason = "Price moved materially opposite to the documented event polarity in the synchronized reaction window."
        elif price_aligned and corroborators > 0 and score >= self.confirm_threshold:
            status = "confirmed"
            reason = "Price moved in the documented direction and at least one independent market dimension corroborated the reaction."
        elif score <= self.contradict_threshold:
            status = "contradicted"
            reason = "Available synchronized market dimensions collectively oppose the documented event polarity."
        else:
            status = "mixed"
            reason = "Synchronized market evidence is mixed or insufficient for a clean confirmation/contradiction classification."

        return MarketConfirmationAssessment(
            event_id=event.event_id,
            status=status,
            expected_direction=expected,
            observed_return=round(observed_return, 6),
            relative_return=round(relative_return, 6) if relative_return is not None else None,
            volume_ratio=round(volume_ratio, 3) if volume_ratio is not None else None,
            vwap_relation=vwap_relation,
            sector_alignment=sector_alignment,
            observations_used=len(baseline_candidates[-1:] + reaction_candidates),
            window_start=baseline.timestamp,
            window_end=terminal.timestamp,
            score=score,
            reason=reason,
        )

    @staticmethod
    def _untested(event: SemanticEvent, expected: str, reason: str) -> MarketConfirmationAssessment:
        return MarketConfirmationAssessment(
            event_id=event.event_id,
            status="untested",
            expected_direction=expected,
            observed_return=None,
            relative_return=None,
            volume_ratio=None,
            vwap_relation="not_available",
            sector_alignment="not_available",
            observations_used=0,
            window_start=None,
            window_end=None,
            score=0.0,
            reason=reason,
        )

    def assess_many(self, events: list[SemanticEvent] | tuple[SemanticEvent, ...], observations: Iterable[MarketObservation]) -> tuple[MarketConfirmationAssessment, ...]:
        materialized = tuple(observations)
        return tuple(self.assess(event, materialized) for event in events)
