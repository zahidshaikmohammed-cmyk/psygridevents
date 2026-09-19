from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .direction import DirectionEngine
from .semantic import SemanticEvent
from .transmission import TransmissionEngine


@dataclass(frozen=True)
class AssetMechanismMapping:
    """CP8: one explicit EVENT -> ASSET -> MECHANISM edge.

    An event can map to more than one asset (e.g. a directly named issuer plus
    a configured sector exposure), so `AssetMechanismEngine.assess` returns a
    tuple. Every field that cannot be established from evidence stays None or
    "unresolved"/"unknown" rather than being guessed.
    """

    event_id: str
    asset: str | None
    asset_type: str  # "instrument" | "sector" | "index" | "unresolved"
    exposure_type: str  # e.g. "direct", "input_cost", "financing_conditions", "unresolved"
    mechanism: str | None
    expected_direction: str  # "positive" | "negative" | "neutral" | "unknown"
    confidence: float
    evidence: tuple[str, ...]
    uncertainty: tuple[str, ...]
    resolved: bool
    basis: str


class AssetMechanismEngine:
    """Bridge a semantic event to the market asset(s) and mechanism it implies.

    This wraps the existing fail-closed TransmissionEngine/ExposureGraph rather
    than re-implementing exposure resolution, and adds exactly one missing
    capability: an explicit, configured macro/sector fallback for events that
    name no configured instrument at all (see config/macro_exposure_rules.yaml).
    Direction is computed once via the shared DirectionEngine so it is never
    re-derived (or re-fabricated) differently by different callers.
    """

    def __init__(
        self,
        exposure_rules_file: str | Path,
        direction_rules_file: str | Path,
        issuer_metadata_file: str | Path | None = None,
        macro_exposure_rules_file: str | Path | None = None,
    ) -> None:
        self.transmission_engine = TransmissionEngine(exposure_rules_file, issuer_metadata_file)
        self.direction_engine = DirectionEngine(direction_rules_file)
        self.macro_channels: dict[str, list[dict[str, Any]]] = {}
        if macro_exposure_rules_file is not None and Path(macro_exposure_rules_file).exists():
            data = yaml.safe_load(Path(macro_exposure_rules_file).read_text(encoding="utf-8")) or {}
            self.macro_channels = data.get("macro_channels", {})

    def assess(self, event: SemanticEvent) -> tuple[AssetMechanismMapping, ...]:
        transmission = self.transmission_engine.assess(event)
        direction = self.direction_engine.assess(event)
        direction_uncertainty = () if direction.direction != "unknown" else (direction.reason,)

        if transmission.status == "blocked":
            return (
                AssetMechanismMapping(
                    event_id=event.event_id,
                    asset=None,
                    asset_type="unresolved",
                    exposure_type="unresolved",
                    mechanism=None,
                    expected_direction=direction.direction,
                    confidence=0.0,
                    evidence=(),
                    uncertainty=(transmission.reason,) + direction_uncertainty,
                    resolved=False,
                    basis="blocked",
                ),
            )

        if transmission.links:
            mappings = []
            for link in transmission.links:
                asset_type = "instrument" if link.relationship == "direct" else "sector"
                mappings.append(
                    AssetMechanismMapping(
                        event_id=event.event_id,
                        asset=link.target,
                        asset_type=asset_type,
                        exposure_type=link.relationship,
                        mechanism=link.mechanism,
                        expected_direction=direction.direction,
                        confidence=round(min(link.confidence, event.extraction_confidence), 3),
                        evidence=direction.evidence,
                        uncertainty=direction_uncertainty,
                        resolved=True,
                        basis=link.basis,
                    )
                )
            return tuple(mappings)

        # No instrument-anchored link. Only a configured, event-type-specific
        # macro/sector fallback may fill this gap; nothing is guessed.
        if not event.instruments:
            channels = self.macro_channels.get(event.event_type, [])
            if channels:
                mappings = []
                for channel in channels:
                    mappings.append(
                        AssetMechanismMapping(
                            event_id=event.event_id,
                            asset=str(channel["asset"]),
                            asset_type=str(channel.get("asset_type", "sector")),
                            exposure_type=str(channel.get("relationship", "macro_exposure")),
                            mechanism=str(channel.get("mechanism")) if channel.get("mechanism") else None,
                            expected_direction=direction.direction,
                            confidence=round(
                                min(float(channel.get("confidence", 0.5)), event.extraction_confidence), 3
                            ),
                            evidence=direction.evidence,
                            uncertainty=(
                                "Sector/index-level exposure inferred from event type only; "
                                "no single configured instrument is implicated.",
                            )
                            + direction_uncertainty,
                            resolved=True,
                            basis="configured_macro_channel",
                        )
                    )
                return tuple(mappings)

        return (
            AssetMechanismMapping(
                event_id=event.event_id,
                asset=None,
                asset_type="unresolved",
                exposure_type="unresolved",
                mechanism=None,
                expected_direction=direction.direction,
                confidence=0.0,
                evidence=(),
                uncertainty=(transmission.reason,) + direction_uncertainty,
                resolved=False,
                basis=transmission.status,
            ),
        )

    def assess_many(
        self, events: list[SemanticEvent] | tuple[SemanticEvent, ...]
    ) -> tuple[tuple[AssetMechanismMapping, ...], ...]:
        return tuple(self.assess(event) for event in events)

    @staticmethod
    def primary(mappings: tuple[AssetMechanismMapping, ...]) -> AssetMechanismMapping | None:
        """Pick the single best-supported mapping for signal purposes.

        Direct instrument exposure is preferred over sector/index exposure;
        among equals, higher confidence wins. Provenance for every candidate
        mapping remains available on the full tuple regardless of this pick.
        """
        if not mappings:
            return None

        def rank(mapping: AssetMechanismMapping) -> tuple[bool, int, float]:
            type_rank = 0 if mapping.exposure_type == "direct" else 1
            return (not mapping.resolved, type_rank, -mapping.confidence)

        return sorted(mappings, key=rank)[0]
