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
    observations: list[RawObservation], *, similarity_threshold: float = 0.93, window_hours: int = 48
) -> list[DuplicateDecision]:
    """Deduplicate observations against every prior unique representative.

    Behaviorally identical to calling exact_key()/near_duplicate() for every
    (observation, representative) pair, but avoids two sources of wasted
    work that dominate the cost at real feed volumes:

    1. canonical_text(title) was recomputed on every single pairwise
       comparison; it is computed once per observation/representative here.
    2. difflib.SequenceMatcher.ratio() is expensive (character-level LCS).
       Its value can never exceed 2*min(len_a, len_b)/(len_a+len_b) (it
       cannot match more characters than the shorter string contains), so
       that exact upper bound is checked first and the real computation is
       skipped whenever it cannot possibly reach similarity_threshold. This
       never changes which pairs are classified as duplicates -- it only
       skips computations whose outcome is already determined.
    """
    decisions: list[DuplicateDecision] = []
    # Each representative: (obs_id, observation, exact_key, canonical_url, canonical_title, title_len)
    representatives: list[tuple[str, RawObservation, tuple[str, str], str, str, int]] = []

    for index, observation in enumerate(observations):
        obs_id = f"obs-{index:08d}"
        duplicate_of: str | None = None
        reason = "unique"
        similarity = 0.0

        obs_url = observation.url.split("#", 1)[0].rstrip("/")
        obs_title = canonical_text(observation.title)
        obs_key = (obs_url, obs_title)
        obs_len = len(obs_title)

        for rep_id, representative, rep_key, rep_url, rep_title, rep_len in representatives:
            if obs_key == rep_key:
                duplicate_of, reason, similarity = rep_id, "exact_url_and_title", 1.0
                break
            if observation.publisher == representative.publisher and obs_url == rep_url:
                duplicate_of, reason, similarity = rep_id, "near_duplicate_title", 1.0
                break
            if observation.published_at and representative.published_at:
                if abs(observation.published_at - representative.published_at) > timedelta(hours=window_hours):
                    continue
            total_len = obs_len + rep_len
            score = 1.0 if total_len == 0 else 2.0 * min(obs_len, rep_len) / total_len
            if score < similarity_threshold:
                continue
            if total_len > 0:
                score = SequenceMatcher(None, obs_title, rep_title).ratio()
                if score < similarity_threshold:
                    continue
            duplicate_of, reason, similarity = rep_id, "near_duplicate_title", score
            break

        decisions.append(DuplicateDecision(observation, duplicate_of, reason, similarity))
        if duplicate_of is None:
            representatives.append((obs_id, observation, obs_key, obs_url, obs_title, obs_len))

    return decisions
