from __future__ import annotations

from dataclasses import dataclass

from .acquisition import RawObservation


@dataclass(frozen=True)
class EvidenceAssessment:
    source_count: int
    independent_publisher_count: int
    best_source_tier: int
    first_party_present: bool
    corroboration_state: str
    notes: tuple[str, ...]


def assess_evidence(observations: list[RawObservation]) -> EvidenceAssessment:
    if not observations:
        return EvidenceAssessment(0, 0, 5, False, "no_evidence", ())

    publishers = {item.publisher.strip().lower() for item in observations if item.publisher.strip()}
    best_tier = min(item.source_tier for item in observations)
    first_party = best_tier == 0

    if first_party and len(publishers) >= 2:
        state = "first_party_plus_independent_corroboration"
    elif first_party:
        state = "first_party_evidence"
    elif len(publishers) >= 3:
        state = "multi_source_secondary_corroboration"
    elif len(publishers) == 2:
        state = "two_source_secondary_corroboration"
    else:
        state = "single_source"

    notes: list[str] = []
    if best_tier > 0:
        notes.append("No first-party source is present in this evidence set.")
    if len(publishers) == 1:
        notes.append("Multiple articles from one publisher do not count as independent corroboration.")

    return EvidenceAssessment(
        source_count=len(observations),
        independent_publisher_count=len(publishers),
        best_source_tier=best_tier,
        first_party_present=first_party,
        corroboration_state=state,
        notes=tuple(notes),
    )
