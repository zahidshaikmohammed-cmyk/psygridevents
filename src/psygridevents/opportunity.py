"""Transparent opportunity_score (0-100). A weighted, explainable heuristic -- NOT a probability.

No calibrated out-of-sample model exists yet, so the score is never called a
probability. Every component, weight, penalty and the resulting "why" lines
are returned with each signal so a human can audit exactly why it ranked.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .event_pipeline import CanonicalEvent
from .exposure_graph import Exposure
from .market_calendar import SessionPhase
from .reaction import ReactionMetrics
from .settings import SignalSettings

WEIGHTS: dict[str, float] = {
    "event_materiality": 14.0,
    "event_novelty": 8.0,
    "source_confidence": 8.0,
    "entity_confidence": 6.0,
    "direction_confidence": 6.0,
    "mechanism_confidence": 5.0,
    "market_reaction": 12.0,
    "relative_strength": 7.0,
    "volume_confirmation": 8.0,
    "vwap_confirmation": 5.0,
    "reaction_speed": 4.0,
    "remaining_opportunity": 10.0,
    "sector_confirmation": 3.0,
    "market_regime": 2.0,
    "data_freshness": 2.0,
}
assert abs(sum(WEIGHTS.values()) - 100.0) < 1e-9

_CONFIRMATION_FACTOR = {
    "CONFIRMED_PRIMARY": 1.0, "CORROBORATED": 0.8, "SINGLE_SOURCE": 0.6, "UNCONFIRMED_DISCOVERY": 0.3,
    "CONTRADICTED": 0.3,
}
_NOVELTY_FACTOR = {"new": 1.0, "updated": 0.75, "follow_up": 0.45, "repeat": 0.1, "stale_repackaged": 0.05}


@dataclass(frozen=True)
class OpportunityScore:
    opportunity_score: float
    raw_score: float
    components: dict[str, dict[str, float]]
    penalties: dict[str, float]
    caps: dict[str, float]
    why: tuple[str, ...]
    risk_flags: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _clamp(value: float | None, low: float = 0.0, high: float = 1.0) -> float:
    if value is None:
        return 0.0
    return max(low, min(high, value))


@dataclass
class ScoreContext:
    phase: SessionPhase
    regime: str
    event_age_hours: float | None
    settings: SignalSettings
    extra_risk_flags: list[str] = field(default_factory=list)


def score_opportunity(
    event: CanonicalEvent,
    exposure: Exposure,
    reaction: ReactionMetrics | None,
    *,
    direction: str,
    direction_confidence: float,
    context: ScoreContext,
) -> OpportunityScore:
    settings = context.settings
    components: dict[str, float] = {}
    why: list[tuple[float, str]] = []
    risk: list[str] = list(context.extra_risk_flags)

    components["event_materiality"] = _clamp(exposure.materiality if exposure.hop else event.materiality_score)
    components["event_novelty"] = _NOVELTY_FACTOR.get(event.novelty_status, 0.6)
    components["source_confidence"] = _clamp(event.source_confidence * _CONFIRMATION_FACTOR.get(event.confirmation_status, 0.5))
    components["entity_confidence"] = _clamp(event.entity_confidence.get(exposure.symbol, exposure.confidence))
    components["direction_confidence"] = _clamp(direction_confidence)
    components["mechanism_confidence"] = 1.0 if exposure.relationship == "DIRECT" else _clamp(exposure.confidence)

    sign = 1.0 if direction == "positive" else -1.0 if direction == "negative" else 0.0
    if reaction is not None and reaction.aligned_move is not None and sign:
        threshold = max(0.002, 2.5 * (reaction.volatility_unit or 0.0015))
        components["market_reaction"] = _clamp(reaction.aligned_move / (3.0 * threshold))
        rs = reaction.relative_strength
        components["relative_strength"] = _clamp((rs * sign) / 0.02) if rs is not None else 0.0
        volume = max(reaction.volume_ratio or 0.0, reaction.relative_volume_historical or 0.0)
        components["volume_confirmation"] = _clamp((volume - 1.0) / 2.0) if volume else 0.0
        components["vwap_confirmation"] = 1.0 if reaction.vwap_aligned else 0.0
        latency = reaction.event_to_price_latency_minutes
        speed = reaction.speed_to_half_peak_minutes
        speed_score = 0.0
        if latency is not None:
            speed_score = _clamp(1.0 - latency / 30.0) * 0.6 + (_clamp(1.0 - (speed or 30.0) / 20.0) * 0.4)
        components["reaction_speed"] = speed_score
        components["remaining_opportunity"] = _clamp(reaction.remaining_opportunity)
        srs = reaction.sector_relative_strength
        components["sector_confirmation"] = _clamp((srs * sign) / 0.015) if srs is not None else 0.0
        components["data_freshness"] = 1.0 if reaction.data_status == "LIVE" else 0.0
    else:
        for name in ("market_reaction", "relative_strength", "volume_confirmation", "vwap_confirmation",
                     "reaction_speed", "remaining_opportunity", "sector_confirmation", "data_freshness"):
            components[name] = 0.0
        if reaction is None or reaction.data_status in ("NO_DATA", "TIME_ERROR"):
            risk.append("no_live_market_data")
    regime_score = 0.5
    if context.regime == "RISK_ON":
        regime_score = 1.0 if direction == "positive" else 0.2
    elif context.regime == "RISK_OFF":
        regime_score = 1.0 if direction == "negative" else 0.2
    components["market_regime"] = regime_score

    raw = sum(WEIGHTS[name] * value for name, value in components.items())

    penalties: dict[str, float] = {}
    caps: dict[str, float] = {}
    if reaction is not None:
        if reaction.exhaustion_level == "EXHAUSTED":
            penalties["exhaustion"] = 0.3
            risk.append("move_exhausted")
        elif reaction.exhaustion_level == "HIGH":
            penalties["exhaustion"] = 0.7
            risk.append("move_extended")
        if reaction.data_status == "STALE":
            penalties["stale_market_data"] = 0.4
            risk.append("stale_market_data")
        if reaction.zero_volume_recent:
            penalties["zero_volume"] = 0.5
            risk.append("zero_volume_recent_bars")
        for flag in reaction.exhaustion_flags:
            if flag in ("late_session", "momentum_fading", "volume_climax_faded", "far_from_vwap"):
                risk.append(flag)
    if event.contradiction_status == "open":
        penalties["open_contradiction"] = 0.6
        risk.append("conflicting_sources")
    if exposure.hop >= 1:
        penalties["second_order"] = settings.second_order_discount
        risk.append(f"indirect_exposure:{exposure.relationship.lower()}")
    if context.phase == SessionPhase.NEAR_CLOSE:
        penalties["near_close"] = 0.8
        risk.append("near_close_no_new_entries")
    if context.event_age_hours is not None and context.event_age_hours > settings.max_event_age_for_entry_hours:
        penalties["old_event"] = 0.6
        risk.append("old_event")
    if event.routine or event.roundup or event.negated or event.modality != "asserted":
        penalties["non_actionable_event"] = 0.0
    if event.confirmation_status in ("UNCONFIRMED_DISCOVERY", "CONTRADICTED"):
        caps["discovery_only"] = settings.discovery_only_max_score
        risk.append("discovery_only_source" if event.confirmation_status == "UNCONFIRMED_DISCOVERY" else "contradicted_story")
    if direction not in ("positive", "negative"):
        caps["no_direction"] = settings.watch_min_score
    if reaction is not None and reaction.direction_source == "market_reaction":
        risk.append("direction_inferred_from_market_reaction")

    score = raw
    for value in penalties.values():
        score *= value
    for value in caps.values():
        score = min(score, value)
    score = round(max(0.0, min(100.0, score)), 2)

    # -------------------------------------------------------- human-readable WHY
    labels = {
        "event_materiality": "material event", "event_novelty": "fresh/new information",
        "source_confidence": f"{event.confirmation_status.lower().replace('_', ' ')} source",
        "entity_confidence": "clear entity match", "direction_confidence": "clear documented direction",
        "mechanism_confidence": "direct exposure" if exposure.hop == 0 else f"{exposure.relationship.lower()} exposure",
        "market_reaction": "immediate price confirmation", "relative_strength": "strong relative strength",
        "volume_confirmation": "abnormal volume", "vwap_confirmation": "price on the right side of VWAP",
        "reaction_speed": "fast reaction", "remaining_opportunity": "move not yet exhausted",
        "sector_confirmation": "outperforming its sector", "market_regime": "supportive market regime",
        "data_freshness": "live market data",
    }
    for name, value in components.items():
        contribution = WEIGHTS[name] * value
        if value >= 0.6:
            why.append((contribution, labels[name]))
    why_lines = tuple(label for _, label in sorted(why, key=lambda item: -item[0])[:6])

    return OpportunityScore(
        opportunity_score=score,
        raw_score=round(raw, 2),
        components={
            name: {"value": round(value, 3), "weight": WEIGHTS[name], "contribution": round(WEIGHTS[name] * value, 2)}
            for name, value in components.items()
        },
        penalties=penalties,
        caps=caps,
        why=why_lines,
        risk_flags=tuple(dict.fromkeys(risk)),
    )
