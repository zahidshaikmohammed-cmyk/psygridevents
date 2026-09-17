from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

from .semantic import SemanticEvent


@dataclass(frozen=True)
class EventMechanismAssessment:
    event_id: str
    status: str
    assets: tuple[str, ...]
    direction: str
    mechanism: str | None
    confidence: float
    reason: str


class EventMechanismEngine:
    """Map asserted events to verified assets and explicit economic mechanisms.

    This stage never invents an issuer. It consumes the instruments already
    resolved by the verified entity resolver and only emits a directional
    hypothesis when the event language contains an explicit configured signal.
    """

    def __init__(self, rules_file: str | Path) -> None:
        data = yaml.safe_load(Path(rules_file).read_text(encoding="utf-8")) or {}
        self.type_rules = data.get("event_types", {})
        self.positive_patterns = tuple(data.get("positive_patterns", []))
        self.negative_patterns = tuple(data.get("negative_patterns", []))

    def assess(self, event: SemanticEvent) -> EventMechanismAssessment:
        if event.negated or event.modality != "asserted":
            return EventMechanismAssessment(
                event_id=event.event_id,
                status="blocked",
                assets=tuple(event.instruments),
                direction="none",
                mechanism=None,
                confidence=0.0,
                reason="Non-asserted or negated event cannot produce a directional mapping.",
            )
        if not event.instruments:
            return EventMechanismAssessment(
                event_id=event.event_id,
                status="unresolved_asset",
                assets=(),
                direction="none",
                mechanism=None,
                confidence=0.0,
                reason="No verified configured instrument was resolved; signal mapping is fail-closed.",
            )

        rule = self.type_rules.get(event.event_type, self.type_rules.get("other", {}))
        mechanism = rule.get("mechanism")
        text = " ".join(
            value for value in (event.trigger, event.direct_effect, event.indirect_effect, event.competitor_effect, event.supply_chain_effect) if value
        )
        direction = self._direction(text, rule)
        if direction == "unknown":
            return EventMechanismAssessment(
                event_id=event.event_id,
                status="asset_resolved_direction_unknown",
                assets=tuple(event.instruments),
                direction=direction,
                mechanism=mechanism,
                confidence=0.50,
                reason="Verified asset resolved, but configured event language does not establish a directional hypothesis.",
            )
        confidence = min(0.95, max(0.50, event.extraction_confidence))
        return EventMechanismAssessment(
            event_id=event.event_id,
            status="mapped",
            assets=tuple(event.instruments),
            direction=direction,
            mechanism=mechanism,
            confidence=round(confidence, 3),
            reason="Verified asset, asserted event, configured mechanism, and explicit directional language are present.",
        )

    def assess_many(self, events: Iterable[SemanticEvent]) -> tuple[EventMechanismAssessment, ...]:
        return tuple(self.assess(event) for event in events)

    def _direction(self, text: str, rule: dict) -> str:
        explicit = str(rule.get("direction", "unknown"))
        if explicit in {"long", "short", "neutral"}:
            return explicit
        positive = any(re.search(pattern, text, re.I) for pattern in self.positive_patterns)
        negative = any(re.search(pattern, text, re.I) for pattern in self.negative_patterns)
        if positive and not negative:
            return "long"
        if negative and not positive:
            return "short"
        if positive and negative:
            return "unknown"
        return "unknown"
