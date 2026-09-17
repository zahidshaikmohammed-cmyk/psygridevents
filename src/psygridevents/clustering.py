from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
import re

from .acquisition import RawObservation
from .normalize import canonical_text

_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "from",
    "by", "at", "as", "is", "are", "was", "were", "its", "into", "after", "over",
}


@dataclass(frozen=True)
class StoryCluster:
    cluster_id: str
    observations: tuple[RawObservation, ...]
    representative: RawObservation
    similarity_basis: str


def _tokens(observation: RawObservation) -> set[str]:
    text = canonical_text(f"{observation.title} {observation.summary}")
    return {token for token in re.findall(r"[a-z0-9]+", text) if token not in _STOP and len(token) > 2}


def _similarity(a: RawObservation, b: RawObservation) -> tuple[float, str]:
    if a.published_at and b.published_at and abs(a.published_at - b.published_at) > timedelta(hours=72):
        return 0.0, "time_window"
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return 0.0, "empty_text"
    jaccard = len(left & right) / len(left | right)
    return jaccard, "token_jaccard"


def cluster_stories(observations: list[RawObservation], threshold: float = 0.42) -> list[StoryCluster]:
    """Cluster coverage into evolving stories using conservative text similarity.

    This is a deterministic baseline, not the final semantic event model. The
    design intentionally allows an LLM/embedding layer later without changing
    the story-cluster contract.
    """
    clusters: list[list[RawObservation]] = []
    for observation in observations:
        best_index = None
        best_score = 0.0
        for index, cluster in enumerate(clusters):
            score = max(_similarity(observation, member)[0] for member in cluster)
            if score > best_score:
                best_score, best_index = score, index
        if best_index is not None and best_score >= threshold:
            clusters[best_index].append(observation)
        else:
            clusters.append([observation])

    result: list[StoryCluster] = []
    for index, members in enumerate(clusters, start=1):
        representative = min(
            members,
            key=lambda item: (item.source_tier, item.published_at or item.observed_at),
        )
        result.append(
            StoryCluster(
                cluster_id=f"story-{index:06d}",
                observations=tuple(members),
                representative=representative,
                similarity_basis="time-bounded token similarity",
            )
        )
    return result
