from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from .evidence import EvidenceAssessment
from .semantic import SemanticEvent
from .transmission import TransmissionAssessment


@dataclass(frozen=True)
class PriorityAssessment:
    event_id: str
    priority_score: float
    priority_class: str
    coverage: float
    component_scores: dict[str, float]
    available_factors: tuple[str, ...]
    missing_factors: tuple[str, ...]
    reason: str


class PriorityEngine:
    """Create an auditable priority score from available event evidence.

    Missing factors are excluded from the weighted mean rather than fabricated
    as zero. Coverage records how much of the configured scoring model was
    actually observed.
    """

    FACTORS = (
        "source_confidence",
        "novelty",
        "surprise",
        "financial_materiality",
        "exposure",
        "market_relevance",
        "persistence",
        "transmission",
    )

    def __init__(self, rules_file: str | Path) -> None:
        data = yaml.safe_load(Path(rules_file).read_text(encoding="utf-8")) or {}
        self.weights = {name: float(value) for name, value in (data.get("weights") or {}).items()}
        missing = set(self.FACTORS) - set(self.weights)
        if missing:
            raise ValueError(f"Priority weights missing factors: {sorted(missing)}")
        total = sum(self.weights.values())
        if total <= 0:
            raise ValueError("Priority weights must sum to a positive value.")
        self.weights = {name: value / total for name, value in self.weights.items()}

        self.source_confidence_by_tier = {
            int(name): float(value)
            for name, value in (data.get("source_confidence_by_tier") or {}).items()
        }
        self.market_relevance_by_type = {
            str(name): float(value)
            for name, value in (data.get("market_relevance_by_event_type") or {}).items()
        }
        self.persistence_by_horizon = {
            str(name): float(value)
            for name, value in (data.get("persistence_by_horizon") or {}).items()
        }
        classes = data.get("classes") or {}
        self.class_thresholds = {
            "critical": float(classes.get("critical", 85.0)),
            "high": float(classes.get("high", 70.0)),
            "medium": float(classes.get("medium", 50.0)),
            "low": float(classes.get("low", 30.0)),
        }
        self.minimum_coverage_for_critical = float(data.get("minimum_coverage_for_critical", 0.90))
        self.minimum_coverage_for_high = float(data.get("minimum_coverage_for_high", 0.75))

    def assess(
        self,
        event: SemanticEvent,
        evidence: EvidenceAssessment,
        transmission: TransmissionAssessment | None = None,
    ) -> PriorityAssessment:
        components: dict[str, float] = {}

        source = self._source_confidence(evidence)
        if source is not None:
            components["source_confidence"] = source

        if event.novelty_status not in {"", "unknown", "not_assessed"}:
            components["novelty"] = self._bounded(event.novelty_score)

        surprise = self._surprise_score(event.surprise_status)
        if surprise is not None:
            components["surprise"] = surprise

        if event.materiality_status not in {"", "unknown", "not_assessed"}:
            components["financial_materiality"] = self._bounded(event.materiality_score)

        exposure = self._exposure_score(transmission)
        if exposure is not None:
            components["exposure"] = exposure

        market_relevance = self.market_relevance_by_type.get(event.event_type)
        if market_relevance is not None:
            components["market_relevance"] = self._bounded(market_relevance)

        persistence = self.persistence_by_horizon.get(event.time_horizon or "unknown")
        if persistence is not None and (event.time_horizon or "") != "unknown":
            components["persistence"] = self._bounded(persistence)

        if transmission is not None and transmission.status not in {"", "unknown", "unlinked", "blocked"}:
            components["transmission"] = self._bounded(transmission.confidence)

        available = tuple(factor for factor in self.FACTORS if factor in components)
        missing = tuple(factor for factor in self.FACTORS if factor not in components)
        available_weight = sum(self.weights[factor] for factor in available)
        if not available_weight:
            score = 0.0
            coverage = 0.0
        else:
            score = 100.0 * sum(
                components[factor] * self.weights[factor] for factor in available
            ) / available_weight
            coverage = available_weight

        priority_class = self._classify(score, coverage)
        reason = (
            f"{len(available)}/{len(self.FACTORS)} priority factors available "
            f"({coverage:.0%} model coverage); missing: "
            f"{', '.join(missing) if missing else 'none'}."
        )
        return PriorityAssessment(
            event_id=event.event_id,
            priority_score=round(score, 2),
            priority_class=priority_class,
            coverage=round(coverage, 3),
            component_scores={key: round(value, 3) for key, value in components.items()},
            available_factors=available,
            missing_factors=missing,
            reason=reason,
        )

    def assess_many(
        self,
        events: Iterable[SemanticEvent],
        evidence: EvidenceAssessment,
        transmissions: Iterable[TransmissionAssessment] = (),
    ) -> tuple[PriorityAssessment, ...]:
        transmission_by_id = {item.event_id: item for item in transmissions}
        return tuple(
            self.assess(event, evidence, transmission_by_id.get(event.event_id))
            for event in events
        )

    def rank(self, assessments: Iterable[PriorityAssessment]) -> list[PriorityAssessment]:
        return sorted(
            assessments,
            key=lambda item: (item.priority_score, item.coverage, item.event_id),
            reverse=True,
        )

    def _classify(self, score: float, coverage: float) -> str:
        if score >= self.class_thresholds["critical"] and coverage >= self.minimum_coverage_for_critical:
            return "critical"
        if score >= self.class_thresholds["high"] and coverage >= self.minimum_coverage_for_high:
            return "high"
        if score >= self.class_thresholds["medium"]:
            return "medium"
        if score >= self.class_thresholds["low"]:
            return "low"
        return "informational"

    def _source_confidence(self, evidence: EvidenceAssessment) -> float | None:
        if evidence.source_count <= 0:
            return None
        return self.source_confidence_by_tier.get(evidence.best_source_tier)

    @staticmethod
    def _surprise_score(status: str) -> float | None:
        return {"surprising_high": 1.0, "surprising_low": 1.0, "in_line": 0.20}.get(status)

    @staticmethod
    def _exposure_score(transmission: TransmissionAssessment | None) -> float | None:
        if transmission is None or not transmission.links:
            return None
        values = []
        for link in transmission.links:
            relationship = link.relationship.lower()
            values.append(link.confidence * (1.0 if relationship == "direct" else 0.75))
        return max(values) if values else None

    @staticmethod
    def _bounded(value: float) -> float:
        return max(0.0, min(1.0, float(value)))
