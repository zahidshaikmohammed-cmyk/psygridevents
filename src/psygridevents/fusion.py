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
    """Publisher + title + leading summary text.

    The summary is part of the key because exchange feeds use the company
    name as the title of every filing; title alone would drop real filings.
    Re-polls of the same item produce the same key (and the URL key also
    catches them).
    """
    title = canonical_text(observation.title)
    summary = canonical_text(observation.summary)[:300]
    basis = f"{observation.publisher.strip().lower()}|{title}|{summary}"
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
    def match_text(self) -> str:
        """Text used for similarity. NSE feed titles are just the company name, so the
        filing subject and summary carry the event identity."""
        subject = (self.raw.get("nse_filing") or {}).get("subject", "")
        if subject or self.quality == SourceQuality.PRIMARY.value:
            return f"{self.title} {subject} {self.summary[:300]}".lower()
        return self.title.lower()

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
    _symbols: set[str] | None = field(default=None, repr=False, compare=False)
    _tokens: frozenset[str] | None = field(default=None, repr=False, compare=False)

    def invalidate(self) -> None:
        self._symbols = None
        self._tokens = None

    @property
    def symbols(self) -> set[str]:
        if self._symbols is None:
            result: set[str] = set()
            for item in self.evidence:
                result.update(item.symbols)
            self._symbols = result
        return self._symbols

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
        if self._tokens is None:
            result: set[str] = set()
            for item in self.evidence[-8:]:
                result |= item.tokens
            self._tokens = frozenset(result)
        return self._tokens

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
        self._by_symbol: dict[str, set[str]] = {}
        self._by_token: dict[str, set[str]] = {}

    def _index(self, story: Story) -> None:
        for symbol in story.symbols:
            self._by_symbol.setdefault(symbol, set()).add(story.story_id)
        for token in story.tokens:
            self._by_token.setdefault(token, set()).add(story.story_id)

    def _candidates(self, evidence: Evidence) -> list[Story]:
        """Stories that could possibly match: shared issuer, or >= 2 shared content tokens."""
        ids: set[str] = set()
        for symbol in evidence.symbols:
            ids |= self._by_symbol.get(symbol, set())
        counts: dict[str, int] = {}
        for token in evidence.tokens:
            for story_id in self._by_token.get(token, ()):
                counts[story_id] = counts.get(story_id, 0) + 1
        ids |= {story_id for story_id, count in counts.items() if count >= 2}
        return [self.stories[story_id] for story_id in ids if story_id in self.stories]

    def expire(self, now: datetime) -> list[str]:
        expired = [story_id for story_id, story in self.stories.items() if now - story.latest_seen > self.window]
        for story_id in expired:
            del self.stories[story_id]
        if expired:
            gone = set(expired)
            for index in (self._by_symbol, self._by_token):
                for key in list(index):
                    index[key] -= gone
                    if not index[key]:
                        del index[key]
        return expired

    def restore(self, stories: Iterable[Story]) -> None:
        for story in stories:
            story.dirty = False
            self.stories[story.story_id] = story
            self._index(story)

    def _score(self, story: Story, evidence: Evidence) -> float:
        if abs((evidence.public_at - story.latest_seen).total_seconds()) > self.window.total_seconds():
            return 0.0
        story_symbols = story.symbols
        recent = story.evidence[-5:]
        left, right = evidence.tokens, story.tokens
        jaccard = len(left & right) / len(left | right) if left and right else 0.0
        if evidence.symbols and story_symbols:
            if not set(evidence.symbols) & story_symbols:
                return 0.0
            family = subtype_family(evidence.subtype)
            families = story.families
            ratio = max(SequenceMatcher(None, evidence.match_text, item.match_text).ratio() for item in recent)
            score = max(jaccard, ratio * 0.9)
            known = family not in ("unclassified", "routine_disclosure")
            if known and families and family in families:
                # Same issuer, same event family, within 12h: one evolving event
                # (e.g. board-meeting outcome + results filing + media coverage).
                if abs((evidence.public_at - story.first_public_at).total_seconds()) <= 12 * 3600:
                    return max(score, 0.6)
                return score
            if known and families and family not in families:
                # Same issuer, different kind of event: separate stories unless near-identical text.
                return score if ratio >= 0.95 else min(score, 0.25)
            return score
        if evidence.symbols or story_symbols:
            # One side names an issuer and the other does not: only near-identical text joins.
            ratio = max(SequenceMatcher(None, evidence.match_text, item.match_text).ratio() for item in recent)
            return ratio if ratio >= self.title_similarity else 0.0
        # Neither side names an issuer (macro/policy/commodity coverage): require strong overlap.
        if jaccard >= 0.5:
            return max(jaccard, self.join_similarity)
        if jaccard < 0.35:
            return 0.0
        matcher_best = 0.0
        for item in recent:
            matcher = SequenceMatcher(None, evidence.match_text, item.match_text)
            if matcher.real_quick_ratio() < 0.8 or matcher.quick_ratio() < 0.8:
                continue
            matcher_best = max(matcher_best, matcher.ratio())
        return matcher_best if matcher_best >= 0.8 else 0.0

    def add(self, evidence: Evidence) -> tuple[Story, bool]:
        """Attach evidence to the best matching open story or open a new one. Returns (story, created)."""
        best: tuple[float, Story] | None = None
        for story in self._candidates(evidence):
            score = self._score(story, evidence)
            if score >= self.join_similarity and (best is None or score > best[0]):
                best = (score, story)
        if best is not None:
            story = best[1]
            if any(item.obs_id == evidence.obs_id for item in story.evidence):
                return story, False
            story.evidence.append(evidence)
            story.invalidate()
            story.dirty = True
            self._index(story)
            self._update_contradictions(story, evidence)
            return story, False
        story_id = "st-" + hashlib.sha1(f"{evidence.obs_id}|{evidence.public_at.isoformat()}".encode()).hexdigest()[:16]
        story = Story(story_id=story_id, evidence=[evidence])
        self.stories[story_id] = story
        self._index(story)
        return story, True

    @staticmethod
    def _update_contradictions(story: Story, evidence: Evidence) -> None:
        """Record source disagreement inside one story instead of silently picking a side."""
        new_direction = evidence.direction
        if evidence.quality in (SourceQuality.PRIMARY.value, SourceQuality.OFFICIAL.value):
            for entry in story.contradictions:
                if entry.get("status") == "open":
                    entry["status"] = "resolved"
                    entry["resolution"] = (
                        f"later primary/official source prevails: {evidence.publisher} ({evidence.subtype}, {new_direction})"
                    )
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
