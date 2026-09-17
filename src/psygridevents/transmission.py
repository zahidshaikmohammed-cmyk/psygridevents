from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .exposure import ExposureGraph, ExposureLink
from .semantic import SemanticEvent


@dataclass(frozen=True)
class TransmissionAssessment:
    event_id: str
    status: str
    links: tuple[ExposureLink, ...]
    confidence: float
    reason: str


class TransmissionEngine:
    """Turn explicit exposure links into an auditable transmission assessment.

    The engine is fail-closed: it never invents sector, competitor, supply-chain,
    or macro transmission without an explicit configured exposure relationship.
    """

    def __init__(self, rules_file: str | Path, issuer_metadata_file: str | Path | None = None) -> None:
        self.graph = ExposureGraph(rules_file, issuer_metadata_file)

    def assess(self, event: SemanticEvent) -> TransmissionAssessment:
        if event.negated or event.modality in {"hypothetical", "planned"}:
            return TransmissionAssessment(
                event_id=event.event_id,
                status="blocked",
                links=(),
                confidence=0.0,
                reason="Non-asserted or negated event cannot create a transmission path.",
            )

        links = tuple(self.graph.map_event(event))
        if not links:
            return TransmissionAssessment(
                event_id=event.event_id,
                status="unlinked",
                links=(),
                confidence=0.0,
                reason="No explicit exposure relationship is configured for this event.",
            )

        direct = any(link.relationship == "direct" for link in links)
        second_order = any(link.relationship != "direct" for link in links)
        if direct and second_order:
            status = "mixed"
        elif direct:
            status = "direct"
        else:
            status = "second_order"

        confidence = round(max(link.confidence for link in links), 3)
        return TransmissionAssessment(
            event_id=event.event_id,
            status=status,
            links=links,
            confidence=confidence,
            reason=f"{len(links)} explicit transmission link(s) supported by configured exposure evidence.",
        )

    def assess_many(self, events: list[SemanticEvent] | tuple[SemanticEvent, ...]) -> tuple[TransmissionAssessment, ...]:
        return tuple(self.assess(event) for event in events)
