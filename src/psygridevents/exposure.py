from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .semantic import SemanticEvent


@dataclass(frozen=True)
class ExposureLink:
    source: str
    target: str
    relationship: str
    mechanism: str
    horizon: str | None
    confidence: float
    basis: str
    evidence_required: bool


class ExposureGraph:
    """Map semantic events to explicitly supported exposure relationships.

    The graph is fail-closed: a relationship is emitted only when it is present
    in the configured issuer/sector graph or is a direct event-to-instrument link.
    Generic market intuition is never silently converted into exposure data.
    """

    def __init__(self, rules_file: str | Path, issuer_file: str | Path | None = None) -> None:
        rules = yaml.safe_load(Path(rules_file).read_text(encoding="utf-8")) or {}
        self.event_channels: dict[str, Any] = rules.get("event_channels", {})
        self.issuer_metadata = self._load_issuer_metadata(issuer_file)

    @staticmethod
    def _load_issuer_metadata(path: str | Path | None) -> dict[str, Any]:
        if path is None or not Path(path).exists():
            return {}
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return data.get("issuers", {})

    def map_event(self, event: SemanticEvent) -> list[ExposureLink]:
        links: list[ExposureLink] = []

        # Direct exposure is factual when the event itself resolves to an instrument.
        for instrument in event.instruments:
            links.append(
                ExposureLink(
                    source=event.event_id,
                    target=instrument,
                    relationship="direct",
                    mechanism="explicitly_named_in_event",
                    horizon=event.time_horizon,
                    confidence=event.extraction_confidence,
                    basis="source_entity_resolution",
                    evidence_required=True,
                )
            )

        # Second-order links require explicit issuer/sector metadata.
        for instrument in event.instruments:
            metadata = self.issuer_metadata.get(instrument, {})
            sectors = metadata.get("sectors", [])
            for sector in sectors:
                for channel in self.event_channels.get(event.event_type, []):
                    if channel.get("sector") != sector:
                        continue
                    links.append(
                        ExposureLink(
                            source=event.event_id,
                            target=sector,
                            relationship=channel.get("relationship", "sector"),
                            mechanism=channel.get("mechanism", "configured_channel"),
                            horizon=event.time_horizon or channel.get("horizon"),
                            confidence=min(event.extraction_confidence, float(channel.get("confidence", 0.70))),
                            basis="explicit_configured_graph",
                            evidence_required=True,
                        )
                    )
        return self._dedupe(links)

    @staticmethod
    def _dedupe(links: list[ExposureLink]) -> list[ExposureLink]:
        seen: set[tuple[str, str, str, str]] = set()
        result: list[ExposureLink] = []
        for link in links:
            key = (link.source, link.target, link.relationship, link.mechanism)
            if key not in seen:
                seen.add(key)
                result.append(link)
        return result
