from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .semantic import SemanticEvent


@dataclass(frozen=True)
class MaterialityAssessment:
    status: str
    score: float
    financial_relevance: str
    exposure: str
    persistence: str
    time_horizon: str | None
    reason: str


class MaterialityEngine:
    """Evidence-gated heuristic assessment of potential financial materiality.

    This engine does not estimate price impact or probability. It only ranks the
    documented financial relevance of an extracted event. Direct instrument
    exposure is required for a material classification; second-order exposure is
    deliberately left to the transmission layer.
    """

    def __init__(self, rules_file: str | Path) -> None:
        data = yaml.safe_load(Path(rules_file).read_text(encoding="utf-8")) or {}
        self.event_weights: dict[str, float] = {
            key: float(value) for key, value in data.get("event_weights", {}).items()
        }
        self.magnitude_bonus = float(data.get("magnitude_bonus", 0.20))
        self.effect_bonus = float(data.get("effect_bonus", 0.15))
        self.novelty_bonus = float(data.get("novelty_bonus", 0.10))
        self.high_threshold = float(data.get("high_threshold", 0.75))
        self.medium_threshold = float(data.get("medium_threshold", 0.45))

    def assess(self, event: SemanticEvent) -> MaterialityAssessment:
        if not event.instruments:
            return MaterialityAssessment(
                status="unknown",
                score=0.0,
                financial_relevance="unresolved",
                exposure="none_verified",
                persistence=event.time_horizon or "unknown",
                time_horizon=event.time_horizon,
                reason="No configured instrument exposure was explicitly resolved.",
            )

        if event.negated or event.modality != "asserted":
            return MaterialityAssessment(
                status="low",
                score=0.0,
                financial_relevance="not_actionable",
                exposure="direct_but_non_asserted",
                persistence=event.time_horizon or "unknown",
                time_horizon=event.time_horizon,
                reason=f"Event language is {event.modality}; non-asserted events cannot receive high materiality.",
            )

        score = self.event_weights.get(event.event_type, 0.35)
        reasons: list[str] = [f"event_type={event.event_type}"]

        if event.magnitude is not None and event.magnitude.normalized_value is not None:
            score += self.magnitude_bonus
            reasons.append("quantified_magnitude")

        if any((event.direct_effect, event.indirect_effect, event.competitor_effect, event.supply_chain_effect)):
            score += self.effect_bonus
            reasons.append("documented_financial_effect")

        if event.novelty_status in {"new", "updated"}:
            score += self.novelty_bonus
            reasons.append(f"novelty={event.novelty_status}")

        score = round(min(0.99, max(0.0, score)), 3)
        if score >= self.high_threshold:
            status = "high"
        elif score >= self.medium_threshold:
            status = "medium"
        else:
            status = "low"

        financial_relevance = "high" if status == "high" else "moderate" if status == "medium" else "limited"
        return MaterialityAssessment(
            status=status,
            score=score,
            financial_relevance=financial_relevance,
            exposure="direct",
            persistence=event.time_horizon or "unspecified",
            time_horizon=event.time_horizon,
            reason="Direct exposure with " + ", ".join(reasons) + ".",
        )

    def assess_many(self, events: list[SemanticEvent] | tuple[SemanticEvent, ...]) -> list[MaterialityAssessment]:
        return [self.assess(event) for event in events]
