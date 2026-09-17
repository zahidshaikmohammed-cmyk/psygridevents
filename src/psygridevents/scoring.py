from __future__ import annotations

from dataclasses import dataclass

from .models import Event, ImpactLevel, IntelligenceAssessment


@dataclass(frozen=True)
class ScoreInputs:
    """Normalized inputs. Keeping these explicit makes the score auditable."""

    source_confidence: float = 0.0
    novelty: float = 0.0
    surprise: float = 0.0
    financial_materiality: float = 0.0
    exposure: float = 0.0
    market_relevance: float = 0.0
    persistence: float = 0.0
    transmission: float = 0.0

    def __post_init__(self) -> None:
        for name, value in self.__dict__.items():
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")


def _impact_level(score: float) -> ImpactLevel:
    if score >= 85:
        return ImpactLevel.CRITICAL
    if score >= 70:
        return ImpactLevel.HIGH
    if score >= 50:
        return ImpactLevel.MEDIUM
    if score >= 30:
        return ImpactLevel.LOW
    return ImpactLevel.INFORMATIONAL


def calculate_priority(event: Event, inputs: ScoreInputs) -> IntelligenceAssessment:
    """Return an auditable priority score; this is not a trading signal."""
    weights = {
        "source_confidence": 0.16,
        "novelty": 0.12,
        "surprise": 0.12,
        "financial_materiality": 0.18,
        "exposure": 0.14,
        "market_relevance": 0.12,
        "persistence": 0.06,
        "transmission": 0.10,
    }
    values = inputs.__dict__
    score = 100.0 * sum(values[key] * weight for key, weight in weights.items())

    return IntelligenceAssessment(
        event_id=event.event_id,
        materiality=_impact_level(score),
        priority_score=round(score, 2),
        novelty_score=inputs.novelty,
        surprise_score=inputs.surprise,
        source_confidence=inputs.source_confidence,
        market_relevance=inputs.market_relevance,
    )
