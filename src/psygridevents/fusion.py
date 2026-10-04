"""News fusion: many reports of one event -> one canonical story with evidence records.

A story keeps every observation as an evidence record (never merged away),
and tracks first_seen / latest_seen / source_count / publisher_count / best
source quality / confirmation status / contradictions. Stories persist in
SQLite and are rehydrated on restart so fusion continues across restarts.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from typing import Any, Iterable

from .acquisition import RawObservation
from .normalize import canonical_text
from .providers.base import QUALITY_RANK, SourceQuality, best_quality

_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with", "from", "by", "at", "as", "is", "are",
    "was", "were", "its", "into", "after", "over", "ltd", "limited", "company", "shares", "share", "stock", "says",
    "said", "india", "indian", "nse", "bse", "inr", "rs", "crore",
}

CONFIRMED_PRIMARY = "CONFIRMED_PRIMARY"
CORROBORATED = "CORROBORATED"
SINGLE_SOURCE = "SINGLE_SOURCE"
UNCONFIRMED_DISCOVERY = "UNCONFIRMED_DISCOVERY"
CONTRADICTED = "CONTRADICTED"


def content_key(observation: RawObservation) -> str:
    title = canonical_text(observation.title)
    basis = f"{observation.publisher.strip().lower()}|{title}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()


def url_key(url: str) -> str:
    return "url:" + hashlib.sha1(url.split("#", 1)[0].rstrip("/").encode("utf-8")).hexdigest()


def tokens(text: str) -> frozenset[str]:
    return frozenset(
        token for token in re.findall(r"[a-z0-9]+", canonical_text(text)) if token not in _STOP and len(token) > 2
    )


@dataclass
class Evidence:
    obs_id: str
    provider_id: str
    publisher: str
    quality: str
    title: str
    url: str
    summary: str
    published_at: datetime | None
    observed_at: datetime
    public_at: datetime
    public_at_basis: str
    symbols: tuple[str, ...]
    entity_confidence: dict[str, float]
    subtype: str
    direction: str
    modality: str
    tokens: frozenset[str]
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ingestion_latency_seconds(self) -> float | None:
        if self.published_at is None:
            return None
        return round((self.observed_at - self.published_at).total_seconds(), 1)


_FAMILY_PREFIXES = (
    ("results_", "results"), ("business_update", "business_update"), ("order_", "order"), ("guidance_", "guidance"),
    ("usfda_", "usfda"), ("credit_rating_", "credit_rating"), ("broker_", "broker"), ("pledge_", "pledge"),
    ("rate_", "rate"), ("crude_", "crude"), ("metals_", "metals"), ("rupee_", "rupee"), ("litigation_", "litigation"),
    ("key_management_", "management"), ("export_", "export"), ("board_meeting_", "results"),
)


def subtype_family(subtype: str) -> str:
    for prefix, family in _FAMILY_PREFIXES:
        if subtype.startswith(prefix):
            return family
    return subtype


@dataclass
class Story:
    story_id: str
    evidence: list[Evidence]
    contradictions: list[dict[str, Any]] = field(default_factory=list)
    dirty: bool = True

    @property
    def symbols(self) -> set[str]:
        result: set[str] = set()
        for item in self.evidence:
            result.update(item.symbols)
        return result

    @property
    def first_seen(self) -> datetime:
        return min(item.observed_at for item in self.evidence)

    @property
    def latest_seen(self) -> datetime:
        return max(item.observed_at for item in self.evidence)

    @property
    def first_public_at(self) -> datetime:
        return min(item.public_at for item in self.evidence)

    @property
    def publishers(self) -> list[str]:
        return sorted({item.publisher.strip().lower() for item in self.evidence if item.publisher.strip()})

    @property
    def best_quality(self) -> str:
        return best_quality([item.quality for item in self.evidence])

    @property
    def families(self) -> set[str]:
        return {subtype_family(item.subtype) for item in self.evidence if item.subtype not in ("unclassified", "routine_disclosure")}

    @property
    def tokens(self) -> frozenset[str]:
        result: set[str] = set()
        for item in self.evidence[-8:]:
            result |= item.tokens
        return frozenset(result)

    @property
    def representative(self) -> Evidence:
        return min(self.evidence, key=lambda item: (QUALITY_RANK.get(item.quality, 9), item.public_at))

    @property
    def confirmation_status(self) -> str:
        if any(item.get("status") == "open" for item in self.contradictions):
            return CONTRADICTED
        qualities = {item.quality for item in self.evidence}
        if qualities & {SourceQuality.PRIMARY.value, SourceQuality.OFFICIAL.value}:
            return CONFIRMED_PRIMARY
        reputable = {item.publisher.lower() for item in self.evidence if item.quality == SourceQuality.REPUTABLE_SECONDARY.value}
        independent = {item.publisher.lower() for item in self.evidence}
        if len(reputable) >= 2 or (len(reputable) >= 1 and len(independent) >= 3):
            return CORROBORATED
        if reputable:
            return SINGLE_SOURCE
        return UNCONFIRMED_DISCOVERY


class StoryBook:
    """In-memory window of open stories (default 72h), persisted by the caller."""

    def __init__(self, *, window_hours: float = 72.0, join_similarity: float = 0.34, title_similarity: float = 0.72) -> None:
        self.window = timedelta(hours=window_hours)
        self.join_similarity = join_similarity
        self.title_similarity = title_similarity
        self.stories: dict[str, Story] = {}

    def expire(self, now: datetime) -> list[str]:
        expired = [story_id for story_id, story in self.stories.items() if now - story.latest_seen > self.window]
        for story_id in expired:
            del self.stories[story_id]
        return expired

    def restore(self, stories: Iterable[Story]) -> None:
        for story in stories:
            story.dirty = False
            self.stories[story.story_id] = story

    def _score(self, story: Story, evidence: Evidence) -> float:
        if abs((evidence.public_at - story.latest_seen).total_seconds()) > self.window.total_seconds():
            return 0.0
        story_symbols = story.symbols
        if evidence.symbols and story_symbols:
            if not set(evidence.symbols) & story_symbols:
                return 0.0
        elif evidence.symbols or story_symbols:
            # One side names an issuer and the other does not: only near-identical titles join.
            best = max(SequenceMatcher(None, evidence.title.lower(), item.title.lower()).ratio() for item in story.evidence[-5:])
            return best if best >= self.title_similarity else 0.0
        family = subtype_family(evidence.subtype)
        families = story.families
        left, right = evidence.tokens, story.tokens
        jaccard = len(left & right) / len(left | right) if left and right else 0.0
        title_ratio = max(SequenceMatcher(None, evidence.title.lower(), item.title.lower()).ratio() for item in story.evidence[-5:])
        score = max(jaccard, title_ratio * 0.9)
        if evidence.symbols and story_symbols and families and family in families:
            # Same issuer, same event family, within 12h: the same evolving event
            # (e.g. board-meeting outcome + results filing + media coverage).
            if abs((evidence.public_at - story.first_public_at).total_seconds()) <= 12 * 3600:
                score = max(score, 0.6)
        elif evidence.symbols and story_symbols and families and family not in families and family not in (
            "unclassified", "routine_disclosure"
        ):
            # Same issuer but a different kind of event: keep separate stories.
            score = min(score, 0.25) if title_ratio < 0.9 else score
        return score

    def add(self, evidence: Evidence) -> tuple[Story, bool]:
        """Attach evidence to the best matching open story or open a new one. Returns (story, created)."""
        best: tuple[float, Story] | None = None
        for story in self.stories.values():
            score = self._score(story, evidence)
            if score >= self.join_similarity and (best is None or score > best[0]):
                best = (score, story)
        if best is not None:
            story = best[1]
            if any(item.obs_id == evidence.obs_id for item in story.evidence):
                return story, False
            story.evidence.append(evidence)
            story.dirty = True
            self._update_contradictions(story, evidence)
            return story, False
        story_id = "st-" + hashlib.sha1(f"{evidence.obs_id}|{evidence.public_at.isoformat()}".encode()).hexdigest()[:16]
        story = Story(story_id=story_id, evidence=[evidence])
        self.stories[story_id] = story
        return story, True

    @staticmethod
    def _update_contradictions(story: Story, evidence: Evidence) -> None:
        """Record source disagreement inside one story instead of silently picking a side."""
        new_direction = evidence.direction
        for other in story.evidence[:-1]:
            conflict = None
            if {new_direction, other.direction} == {"positive", "negative"} and subtype_family(evidence.subtype) == subtype_family(other.subtype):
                conflict = "opposite_direction"
            if {evidence.modality, other.modality} & {"negated"} and evidence.modality != other.modality:
                conflict = "denial_vs_claim"
            if (
                {evidence.subtype, other.subtype} >= {"order_win", "order_cancellation"}
                or {evidence.subtype, other.subtype} >= {"usfda_favourable", "usfda_adverse"}
                or {evidence.subtype, other.subtype} >= {"competition_approval", "regulatory_penalty"}
            ):
                conflict = "event_reversal"
            if conflict is None:
                continue
            key = tuple(sorted((evidence.obs_id, other.obs_id)))
            if any(tuple(sorted(item["between"])) == key for item in story.contradictions):
                continue
            primary_new = evidence.quality in (SourceQuality.PRIMARY.value, SourceQuality.OFFICIAL.value)
            primary_old = other.quality in (SourceQuality.PRIMARY.value, SourceQuality.OFFICIAL.value)
            entry = {
                "type": conflict,
                "between": [other.obs_id, evidence.obs_id],
                "detail": f"{other.publisher}: '{other.title[:120]}' vs {evidence.publisher}: '{evidence.title[:120]}'",
                "status": "open",
                "resolution": None,
                "detected_at": evidence.observed_at.isoformat(),
            }
            if primary_new != primary_old:
                winner = evidence if primary_new else other
                entry["status"] = "resolved"
                entry["resolution"] = f"primary/official source prevails: {winner.publisher} ({winner.direction})"
            story.contradictions.append(entry)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
