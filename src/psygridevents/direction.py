from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .semantic import SemanticEvent


@dataclass(frozen=True)
class DirectionAssessment:
    """Evidence-based expected market direction for a semantic event.

    This is shared by CP8 (asset/mechanism mapping), CP9 (event state) and CP11
    (signal engine) so "expected direction" has exactly one implementation
    instead of being silently re-derived (and possibly re-fabricated) in each
    consumer. It never looks at price/market data; that is CP10/CP5's job.
    """

    direction: str  # "positive" | "negative" | "neutral" | "unknown"
    basis: str
    evidence: tuple[str, ...]
    reason: str


class DirectionEngine:
    """Classify the documented effect polarity of a semantic event.

    Fail-closed: an event that is negated, non-asserted, or whose documented
    effects/trigger do not contain an unambiguous polarity signal is "unknown"
    or "neutral" rather than being forced to a side.
    """

    def __init__(self, rules_file: str | Path) -> None:
        data = yaml.safe_load(Path(rules_file).read_text(encoding="utf-8")) or {}
        self.positive_patterns = tuple(data.get("positive_patterns", []))
        self.negative_patterns = tuple(data.get("negative_patterns", []))

    def assess(self, event: SemanticEvent) -> DirectionAssessment:
        if event.negated or event.modality != "asserted":
            return DirectionAssessment(
                direction="unknown",
                basis="not_assessed",
                evidence=(),
                reason=f"Event language is {event.modality}{' and negated' if event.negated else ''}; "
                "expected direction is not assessed for non-asserted events.",
            )

        effect_fields = (
            ("direct_effect", event.direct_effect),
            ("indirect_effect", event.indirect_effect),
            ("competitor_effect", event.competitor_effect),
            ("supply_chain_effect", event.supply_chain_effect),
        )
        for name, value in effect_fields:
            direction = self._classify(value)
            if direction != "neutral":
                return DirectionAssessment(
                    direction=direction,
                    basis=name,
                    evidence=(value,) if value else (),
                    reason=f"Documented {name.replace('_', ' ')} carries {direction} polarity.",
                )

        trigger_direction = self._classify(event.trigger)
        if trigger_direction != "neutral":
            return DirectionAssessment(
                direction=trigger_direction,
                basis="trigger",
                evidence=(event.trigger,) if event.trigger else (),
                reason=f"Event trigger language carries {trigger_direction} polarity.",
            )

        return DirectionAssessment(
            direction="neutral",
            basis="none",
            evidence=(),
            reason="No documented effect or trigger language carries an unambiguous polarity signal.",
        )

    def _classify(self, text: str | None) -> str:
        if not text:
            return "neutral"
        positive = any(re.search(pattern, text, re.IGNORECASE) for pattern in self.positive_patterns)
        negative = any(re.search(pattern, text, re.IGNORECASE) for pattern in self.negative_patterns)
        if positive and not negative:
            return "positive"
        if negative and not positive:
            return "negative"
        return "neutral"
