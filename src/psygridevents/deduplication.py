from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from difflib import SequenceMatcher

from .acquisition import RawObservation
from .normalize import canonical_text


@dataclass(frozen=True)
class DuplicateDecision:
    observation: RawObservation
    duplicate_of: str | None
    reason: str
    similarity: float


def exact_key(observation: RawObservation) -> tuple[str, str]:
    return observation.url.split("#", 1)[0].rstrip("/"), canonical_text(observation.title)


def near_duplicate(a: RawObservation, b: RawObservation, *, window_hours: int = 48) -> float:
    if a.publisher == b.publisher and a.url.split("#", 1)[0].rstrip("/") == b.url.split("#", 1)[0].rstrip("/"):
        return 1.0
    if a.published_at and b.published_at:
        if abs(a.published_at - b.published_at) > timedelta(hours=window_hours):
            return 0.0
    left = canonical_text(a.title)
    right = canonical_text(b.title)
    return SequenceMatcher(None, left, right).ratio()


def deduplicate(
    observations: list[RawObservation], *, similarity_threshold: float = 0.93
) -> list[DuplicateDecision]:
    decisions: list[DuplicateDecision] = []
    representatives: list[tuple[str, RawObservation]] = []

    for index, observation in enumerate(observations):
        obs_id = f"obs-{index:08d}"
        duplicate_of: str | None = None
        reason = "unique"
        similarity = 0.0

        for rep_id, representative in representatives:
            if exact_key(observation) == exact_key(representative):
                duplicate_of = rep_id
                reason = "exact_url_and_title"
                similarity = 1.0
                break
            score = near_duplicate(observation, representative)
            if score >= similarity_threshold:
                duplicate_of = rep_id
                reason = "near_duplicate_title"
                similarity = score
                break

        decisions.append(DuplicateDecision(observation, duplicate_of, reason, similarity))
        if duplicate_of is None:
            representatives.append((obs_id, observation))

    return decisions
