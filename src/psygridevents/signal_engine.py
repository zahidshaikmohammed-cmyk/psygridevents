from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from .asset_mechanism import AssetMechanismMapping
from .event_timing import EventTimingAssessment
from .exhaustion import ExhaustionAssessment
from .market_confirmation import MarketConfirmationAssessment
from .market_response import MarketResponseAssessment
from .semantic import SemanticEvent

SIGNAL_STATES = (
    "NO_SIGNAL",
    "WATCH",
    "EARLY_LONG",
    "EARLY_SHORT",
    "CONFIRMED",
    "INVALIDATED",
    "EXHAUSTED",
)


@dataclass(frozen=True)
class SignalAssessment:
    """CP11: the final event-driven signal contract.

    Every field that cannot be established from available evidence is left
    as None/"unknown"/"unresolved" rather than fabricated. `signal_strength_or_confidence`
    is computed independently of CP6's `priority_score` (see SignalEngine._confidence);
    priority answers "how important is this event", this answers "is there
    currently a sufficiently supported early market opportunity".
    """

    signal_id: str
    event_id: str
    story_id: str
    timestamp: datetime
    asset: str | None
    asset_type: str
    event_type: str
    event_age: float | None
    event_state: str
    expected_direction: str
    signal_state: str
    signal_strength_or_confidence: float
    materiality: str
    transmission_mechanism: str | None
    market_response: str
    price_displacement: float | None
    relative_performance: float | None
    volume_state: str
    vwap_state: str
    exhaustion_state: str
    trigger: str
    invalidation: str
    evidence: tuple[str, ...]
    uncertainty: tuple[str, ...]
    source_references: tuple[str, ...]


class SignalEngine:
    """Combine event, asset/mechanism, timing, market response and exhaustion
    evidence into one explicit, explainable signal state.

    This is intentionally the only place a directional trading-style signal
    state is produced. It never uses future observations relative to `as_of`
    (its inputs already enforce that boundary) and it never substitutes the
    CP6 priority score for signal strength.
    """

    def assess(
        self,
        event: SemanticEvent,
        *,
        story_id: str,
        asset_mapping: AssetMechanismMapping | None,
        timing: EventTimingAssessment,
        response: MarketResponseAssessment | None,
        exhaustion: ExhaustionAssessment | None,
        confirmation: MarketConfirmationAssessment | None,
        as_of: datetime | None = None,
    ) -> SignalAssessment:
        current = as_of or datetime.now(timezone.utc)
        source_references = tuple(dict.fromkeys(span.observation_url for span in event.evidence))
        uncertainty: list[str] = list(event.uncertainty)

        materiality_status = event.materiality_status if event.materiality_status not in ("", "not_assessed") else "unknown"
        materiality_score = event.materiality_score
        exhaustion_state = exhaustion.state if exhaustion else "unknown"
        market_response_state = response.response_state if response else "unknown"
        confirmation_status = confirmation.status if confirmation else "untested"

        asset = asset_mapping.asset if asset_mapping else None
        asset_type = asset_mapping.asset_type if asset_mapping else "unresolved"
        mechanism = asset_mapping.mechanism if asset_mapping else None
        direction = asset_mapping.expected_direction if asset_mapping else "unknown"

        common = dict(
            event_id=event.event_id,
            story_id=story_id,
            timestamp=current,
            asset=asset,
            asset_type=asset_type,
            event_type=event.event_type,
            event_age=timing.age_hours,
            event_state=timing.state,
            expected_direction=direction,
            materiality=materiality_status,
            transmission_mechanism=mechanism,
            market_response=market_response_state,
            price_displacement=response.price_displacement if response else None,
            relative_performance=response.relative_performance if response else None,
            volume_state=response.volume_state if response else "unavailable",
            vwap_state=response.vwap_state if response else "unavailable",
            exhaustion_state=exhaustion_state,
            source_references=source_references,
        )

        # --- Gate 1: minimum evidence required for ANY asset-specific signal ---
        if event.negated or event.modality != "asserted":
            uncertainty.append(
                f"Event language is {event.modality}{' and negated' if event.negated else ''}."
            )
            return self._build(
                common, current, "NO_SIGNAL", 0.0,
                trigger="Event is non-asserted or negated; no signal can be raised on unconfirmed intent.",
                invalidation="N/A", evidence=(), uncertainty=uncertainty,
            )

        if asset_mapping is None or not asset_mapping.resolved or not asset:
            uncertainty.append("No configured asset (instrument, sector, or index) could be resolved for this event.")
            return self._build(
                common, current, "NO_SIGNAL", 0.0,
                trigger="Asset is unresolved.",
                invalidation="N/A", evidence=(), uncertainty=uncertainty,
            )

        if not mechanism:
            uncertainty.append("No supported transmission mechanism is available for the resolved asset.")
            return self._build(
                common, current, "NO_SIGNAL", 0.0,
                trigger="No supported transmission mechanism.",
                invalidation="N/A", evidence=(), uncertainty=uncertainty,
            )

        if direction not in ("positive", "negative"):
            uncertainty.append(
                f"Expected direction is '{direction}'; a directional signal requires positive or negative evidence."
            )
            return self._build(
                common, current, "NO_SIGNAL", 0.0,
                trigger="Expected direction is not established.",
                invalidation="N/A", evidence=(), uncertainty=uncertainty,
            )

        if materiality_status in ("unknown", "", None):
            uncertainty.append("Materiality has not been established for this event.")
            return self._build(
                common, current, "NO_SIGNAL", 0.0,
                trigger="Materiality is unknown.",
                invalidation="N/A", evidence=(), uncertainty=uncertainty,
            )

        uncertainty.extend(asset_mapping.uncertainty)

        # --- Gate 2: exhaustion overrides everything else ---
        if exhaustion_state == "exhausted":
            reason = exhaustion.reason if exhaustion else "insufficient staging evidence"
            return self._build(
                common, current, "EXHAUSTED", 0.0,
                trigger=f"Event-driven move on {asset} already appears exhausted ({reason}).",
                invalidation="N/A: no fresh directional entry is proposed; re-evaluate only on a new material event.",
                evidence=exhaustion.evidence if exhaustion else (), uncertainty=uncertainty,
            )

        # --- Gate 3: independent market contradiction invalidates ---
        if confirmation_status == "contradicted":
            reason = confirmation.reason if confirmation else ""
            return self._build(
                common, current, "INVALIDATED", 0.0,
                trigger=(
                    f"Synchronized market reaction on {asset} moved opposite to the documented "
                    f"{direction} event polarity ({reason})."
                ),
                invalidation="Already invalidated by the observed contradicting market reaction.",
                evidence=(), uncertainty=uncertainty,
            )

        # --- Gate 4: no market response evidence yet -> WATCH ---
        if response is None or market_response_state in ("no_response", "unknown"):
            strength = self._confidence(asset_mapping, materiality_score, corroborated=False)
            return self._build(
                common, current, "WATCH", strength,
                trigger=(
                    f"Material {event.event_type} event resolved to {asset} with a supported "
                    f"{mechanism} mechanism and {direction} expected direction, but no sufficient "
                    "market response evidence is available yet."
                ),
                invalidation=(
                    f"Would be invalidated by a subsequent price/volume reaction opposite to {direction}, "
                    "or superseded if the event is contradicted by a later source."
                ),
                evidence=(), uncertainty=uncertainty,
            )

        # --- Gate 5: independent confirmation ---
        if confirmation_status == "confirmed":
            reason = confirmation.reason if confirmation else ""
            strength = self._confidence(asset_mapping, materiality_score, corroborated=True, response=response)
            return self._build(
                common, current, "CONFIRMED", strength,
                trigger=f"Market reaction on {asset} independently corroborated the documented {direction} event polarity ({reason}).",
                invalidation="Invalidated by a subsequent reversal below the confirmed reaction level, or a contradicting source update.",
                evidence=(), uncertainty=uncertainty,
            )

        # --- Gate 6: early/developing response, not yet independently confirmed ---
        if market_response_state in ("early", "developing"):
            signal_state = "EARLY_LONG" if direction == "positive" else "EARLY_SHORT"
            strength = self._confidence(asset_mapping, materiality_score, corroborated=False, response=response)
            return self._build(
                common, current, signal_state, strength,
                trigger=(
                    f"Early {market_response_state} {direction} price response on {asset} "
                    f"({response.price_displacement:+.4f} since event-time baseline) is aligned with the "
                    f"documented {mechanism} mechanism, and the move is not yet exhausted."
                ),
                invalidation=(
                    f"Invalidated if price reverses through the event-time baseline, or if the aligned move "
                    f"retraces materially from its peak ({response.peak_aligned_displacement:+.4f})."
                ),
                evidence=(), uncertainty=uncertainty,
            )

        # --- market_response_state == "late": not exhausted, not confirmed/contradicted ---
        strength = self._confidence(asset_mapping, materiality_score, corroborated=False, response=response)
        return self._build(
            common, current, "WATCH", strength,
            trigger=(
                f"Price response on {asset} has progressed into the late stage without independent "
                "corroboration; a fresh early entry is no longer appropriate but the event remains relevant."
            ),
            invalidation="Would be superseded by exhaustion (further retracement) or by independent market confirmation.",
            evidence=(), uncertainty=uncertainty,
        )

    def _build(
        self,
        common: dict,
        as_of: datetime,
        signal_state: str,
        strength: float,
        *,
        trigger: str,
        invalidation: str,
        evidence: tuple[str, ...],
        uncertainty: list[str],
    ) -> SignalAssessment:
        return SignalAssessment(
            signal_id=self._signal_id(common["event_id"], as_of),
            signal_state=signal_state,
            signal_strength_or_confidence=round(max(0.0, min(1.0, strength)), 3),
            trigger=trigger,
            invalidation=invalidation,
            evidence=tuple(evidence),
            uncertainty=tuple(dict.fromkeys(uncertainty)),
            **common,
        )

    @staticmethod
    def _confidence(
        asset_mapping: AssetMechanismMapping,
        materiality_score: float,
        *,
        corroborated: bool,
        response: MarketResponseAssessment | None = None,
    ) -> float:
        """Bespoke, priority-independent signal confidence.

        Deliberately does not read PriorityAssessment/priority_score at all,
        so the signal cannot silently collapse into the CP6 importance score.
        """
        response_component = 1.0 if corroborated else 0.0
        if not corroborated and response is not None and response.price_displacement is not None:
            response_component = max(0.0, min(1.0, abs(response.price_displacement) / 0.02))
        return (0.45 * asset_mapping.confidence) + (0.30 * materiality_score) + (0.25 * response_component)

    @staticmethod
    def _signal_id(event_id: str, as_of: datetime) -> str:
        as_of_utc = as_of if as_of.tzinfo else as_of.replace(tzinfo=timezone.utc)
        key = f"{event_id}|{as_of_utc.isoformat()}"
        return "signal-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
