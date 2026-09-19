from __future__ import annotations

from dataclasses import dataclass

from .event_timing import EventTimingAssessment
from .market_response import MarketResponseAssessment

EXHAUSTION_STATES = ("early", "developing", "late", "exhausted", "unknown")

# Both the pure-timing state (CP9) and the price-based response state (CP10)
# are staged on the same early -> developing -> late -> exhausted axis. The
# final exhaustion state is the *later* (more advanced) of the two stages
# whenever both are known, so neither a stale-but-quiet event nor a fresh
# event with an already-extended price move can look "early" by only
# consulting one dimension. This is deliberately not a single percentage
# threshold: it folds in event age, price displacement stage, reversal,
# volume and VWAP behavior (each individually visible on the two input
# assessments and echoed into `evidence` below).
_TIMING_RANK = {"new": 0, "early": 0, "developing": 1, "late": 2, "exhausted": 3}
_RESPONSE_RANK = {"early": 0, "developing": 1, "late": 2, "exhausted": 3}
_RANK_TO_STATE = {0: "early", 1: "developing", 2: "late", 3: "exhausted"}


@dataclass(frozen=True)
class ExhaustionAssessment:
    event_id: str
    state: str
    timing_state: str
    market_response_state: str
    reversal: bool
    evidence: tuple[str, ...]
    uncertainty: tuple[str, ...]
    reason: str


class ExhaustionEngine:
    """CP9+CP10 composite: is this event-driven move still early, or already spent?"""

    def assess(
        self,
        timing: EventTimingAssessment,
        response: MarketResponseAssessment | None,
    ) -> ExhaustionAssessment:
        timing_rank = _TIMING_RANK.get(timing.state)
        response_state = response.response_state if response is not None else "unknown"
        response_rank = _RESPONSE_RANK.get(response_state)

        evidence: list[str] = [f"event_timing_state={timing.state}", f"market_response_state={response_state}"]
        uncertainty = list(timing.uncertainty)
        if response is not None:
            if response.reversal:
                evidence.append("reversal_detected_from_peak_displacement")
            if response.price_displacement is not None:
                evidence.append(f"price_displacement={response.price_displacement:.4f}")
            if response.peak_aligned_displacement is not None:
                evidence.append(f"peak_aligned_displacement={response.peak_aligned_displacement:.4f}")
            if response.volume_state != "unavailable":
                evidence.append(f"volume_state={response.volume_state}")
            if response.vwap_state != "unavailable":
                evidence.append(f"vwap_state={response.vwap_state}")
            if response.velocity_state != "unavailable":
                evidence.append(f"velocity_state={response.velocity_state}")
            uncertainty.extend(response.uncertainty)
        else:
            uncertainty.append("No market observations were supplied; exhaustion is based on event timing only.")
        if timing.age_hours is not None:
            evidence.append(f"event_age_hours={timing.age_hours}")

        reversal = bool(response.reversal) if response is not None else False

        if timing_rank is None and response_rank is None:
            return ExhaustionAssessment(
                event_id=timing.event_id,
                state="unknown",
                timing_state=timing.state,
                market_response_state=response_state,
                reversal=reversal,
                evidence=tuple(evidence),
                uncertainty=tuple(uncertainty),
                reason="Neither event timing nor market response evidence is available.",
            )

        ranks = [rank for rank in (timing_rank, response_rank) if rank is not None]
        final_rank = max(ranks)
        state = _RANK_TO_STATE[final_rank]

        driver = "market response" if (response_rank or 0) >= (timing_rank or 0) else "event timing"
        reason = (
            f"Exhaustion state is '{state}', driven primarily by {driver} "
            f"(timing={timing.state}, market_response={response_state})."
        )

        return ExhaustionAssessment(
            event_id=timing.event_id,
            state=state,
            timing_state=timing.state,
            market_response_state=response_state,
            reversal=reversal,
            evidence=tuple(evidence),
            uncertainty=tuple(uncertainty),
            reason=reason,
        )
