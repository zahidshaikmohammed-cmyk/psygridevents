from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .acquisition import RSSAcquirer, RawObservation
from .clustering import StoryCluster, cluster_stories
from .deduplication import deduplicate
from .entity_resolution import InstrumentResolver, EntityMatch
from .evidence import EvidenceAssessment, assess_evidence
from .normalize import normalized_observation


@dataclass(frozen=True)
class StoryIntelligence:
    story: StoryCluster
    entities: tuple[EntityMatch, ...]
    evidence: EvidenceAssessment


class StoryEngine:
    """Acquisition-to-story pipeline; interpretation is intentionally separate."""

    def __init__(self, instrument_file: str | Path) -> None:
        self.acquirer = RSSAcquirer()
        self.resolver = InstrumentResolver.from_instrument_file(instrument_file)

    def acquire(self, feeds: list[dict], since: datetime | None = None) -> list[RawObservation]:
        observations: list[RawObservation] = []
        for feed in feeds:
            if not feed.get("enabled") or feed.get("mode") == "catalogue":
                continue
            observations.extend(
                self.acquirer.fetch(
                    provider_id=feed["provider_id"],
                    publisher=feed["publisher"],
                    url=feed["url"],
                    source_tier=int(feed["tier"]),
                    since=since,
                )
            )
        return [normalized_observation(item) for item in observations]

    def build_stories(self, observations: list[RawObservation]) -> list[StoryIntelligence]:
        decisions = deduplicate(observations)
        unique = [decision.observation for decision in decisions if decision.duplicate_of is None]
        stories = cluster_stories(unique)
        return [self._intelligence(story) for story in stories]

    def _intelligence(self, story: StoryCluster) -> StoryIntelligence:
        text = " ".join(
            f"{item.title} {item.summary}" for item in story.observations
        )
        matches = tuple(self.resolver.resolve(text))
        return StoryIntelligence(
            story=story,
            entities=matches,
            evidence=assess_evidence(list(story.observations)),
        )

    @staticmethod
    def now() -> datetime:
        return datetime.now(timezone.utc)
