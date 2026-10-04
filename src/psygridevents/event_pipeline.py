"""Observation -> evidence -> canonical event: resolution, classification, extraction,
materiality, novelty, contradiction, direction and exposure (deterministic, no LLM required)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any

from .acquisition import RawObservation
from .contradiction import ContradictionEngine
from .direction import DirectionEngine
from .entity_resolution import EntityResolver
from .event_classifier import UNCLASSIFIED, EventClassifier
from .exposure_graph import Exposure, ExposureGraph990, ExposureRequest
from .fusion import Evidence, Story, content_key, tokens
from .market_calendar import MarketCalendar
from .materiality import MaterialityEngine
from .novelty import NoveltyEngine
from .providers.base import QUALITY_CONFIDENCE, SourceQuality
from .semantic import EvidenceSpan, SemanticEvent, SemanticExtractor

_ROUNDUP = re.compile(
    r"(?i)(stocks? to watch|stocks? in (the )?news|buzzing stocks|top (gainers|losers)|market (live|wrap|highlights|closing)"
    r"|sensex,? nifty (today|live|end|close|open)|stock market (today|live|highlights|update)|trade setup|"
    r"stocks? (that|which) (moved|hit)|market outlook|share price today live)"
)
_CRORE = re.compile(r"(?i)(?:₹|rs\.?|inr)\s?([0-9][0-9,]*(?:\.[0-9]+)?)\s*(crore|cr\b|lakh crore|billion|bn|million|mn)")


def _crore_value(text: str) -> float | None:
    """Rupee magnitude in crore if the text states one explicitly (no FX conversion is guessed)."""
    best = None
    for match in _CRORE.finditer(text):
        try:
            number = float(match.group(1).replace(",", ""))
        except ValueError:
            continue
        unit = match.group(2).lower()
        if unit == "lakh crore":
            value = number * 100000
        elif unit in ("crore", "cr"):
            value = number
        elif unit in ("billion", "bn"):
            value = number * 100  # ₹1 billion = ₹100 crore
        else:
            value = number / 10  # ₹1 million = ₹0.1 crore
        best = value if best is None else max(best, value)
    return best


@dataclass
class CanonicalEvent:
    event_id: str
    story_id: str
    headline: str
    summary: str
    event_type: str
    subtype: str
    secondary_subtypes: tuple[str, ...]
    routine: bool
    roundup: bool
    symbols: tuple[str, ...]
    entity_confidence: dict[str, float]
    entity_basis: dict[str, str]
    direction: str
    direction_confidence: float
    direction_basis: str
    materiality_score: float
    materiality_status: str
    materiality_reason: str
    magnitude_text: str | None
    magnitude_crore: float | None
    novelty_status: str
    novelty_score: float
    novelty_reason: str | None
    modality: str
    negated: bool
    source_quality: str
    source_confidence: float
    confirmation_status: str
    source_count: int
    publisher_count: int
    publishers: tuple[str, ...]
    source_urls: tuple[str, ...]
    public_at: datetime
    public_at_basis: str
    first_seen: datetime
    latest_seen: datetime
    ingestion_latency_seconds: float | None
    session_relation: str
    contradiction_status: str
    contradictions: list[dict[str, Any]]
    factor: str | None
    factor_direction: str | None
    exposures: list[Exposure]
    semantic_event: SemanticEvent
    uncertainty: tuple[str, ...] = ()
    ai_assist: dict[str, Any] | None = None

    @property
    def actionable_quality(self) -> bool:
        """Discovery-only, unconfirmed evidence can never become a confirmed trading event by itself."""
        return self.confirmation_status not in ("UNCONFIRMED_DISCOVERY",)

    def to_api(self, *, include_exposures: bool = True) -> dict[str, Any]:
        data = {
            key: value for key, value in asdict(self).items() if key not in {"semantic_event", "exposures"}
        }
        for key, value in list(data.items()):
            if isinstance(value, datetime):
                data[key] = value.isoformat()
        if include_exposures:
            data["exposures"] = [item.to_dict() for item in self.exposures]
        return data

    def to_record(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id, "story_id": self.story_id, "event_type": self.event_type, "subtype": self.subtype,
            "headline": self.headline, "symbols": list(self.symbols), "direction": self.direction,
            "direction_confidence": self.direction_confidence, "direction_basis": self.direction_basis,
            "materiality": self.materiality_status, "materiality_score": self.materiality_score,
            "novelty_status": self.novelty_status, "novelty_score": self.novelty_score, "modality": self.modality,
            "negated": self.negated, "source_quality": self.source_quality,
            "confirmation_status": self.confirmation_status, "public_at": self.public_at, "first_seen": self.first_seen,
            "session_phase_at_event": self.session_relation, "payload": self.to_api(include_exposures=False),
        }


class EventBuilder:
    def __init__(
        self,
        *,
        resolver: EntityResolver,
        classifier: EventClassifier,
        extractor: SemanticExtractor,
        direction_engine: DirectionEngine,
        materiality_engine: MaterialityEngine,
        novelty_engine: NoveltyEngine,
        contradiction_engine: ContradictionEngine,
        graph: ExposureGraph990,
        calendar: MarketCalendar,
    ) -> None:
        self.resolver = resolver
        self.classifier = classifier
        self.extractor = extractor
        self.direction_engine = direction_engine
        self.materiality_engine = materiality_engine
        self.novelty_engine = novelty_engine
        self.contradiction_engine = contradiction_engine
        self.graph = graph
        self.calendar = calendar

    # ------------------------------------------------------------ evidence
    def evidence_from(self, observation: RawObservation, *, obs_id: str | None = None) -> Evidence:
        raw = observation.raw or {}
        filing = raw.get("nse_filing") or {}
        deal = raw.get("nse_deal") or {}
        text = f"{observation.title}. {observation.summary}".strip()
        if filing.get("subject"):
            text = f"{text}. {filing['subject']}"
        hint = filing.get("symbol") or deal.get("symbol")
        entities = self.resolver.resolve(text, nse_filing_symbol=hint)
        symbols = tuple(entity.symbol for entity in entities)
        confidence = {entity.symbol: entity.confidence for entity in entities}
        classification, _ = self.classifier.classify(observation.title, f"{observation.summary} {filing.get('subject', '')}")
        if deal and classification.subtype == "unclassified":
            classification = replace(
                classification, subtype="promoter_stake_sale" if "sell" in str(deal).lower() else "insider_buy",
                event_type="capital_action", direction="unknown", materiality=0.35,
            )
        public_at, basis = observation.observed_at, "first_seen"
        if filing.get("disseminated_at"):
            try:
                disseminated = datetime.fromisoformat(filing["disseminated_at"])
                if disseminated <= observation.observed_at:
                    public_at, basis = disseminated, "nse_dissemination_timestamp"
            except ValueError:
                pass
        elif observation.published_at is not None and observation.published_at <= observation.observed_at:
            public_at, basis = observation.published_at, "source_published_at"
        modality = self.extractor._modality(observation.title)
        negated = any(re.search(pattern, observation.title, re.I) for pattern in self.extractor.negation_patterns)
        if negated and modality == "asserted":
            modality = "negated"
        return Evidence(
            obs_id=obs_id or content_key(observation), provider_id=observation.provider_id,
            publisher=observation.publisher, quality=observation.source_quality or SourceQuality.DISCOVERY_ONLY.value,
            title=observation.title, url=observation.url, summary=observation.summary[:1500],
            published_at=observation.published_at, observed_at=observation.observed_at, public_at=public_at,
            public_at_basis=basis, symbols=symbols, entity_confidence=confidence,
            subtype=classification.subtype, direction=classification.direction, modality=modality,
            tokens=tokens(f"{observation.title} {observation.summary}"), raw={
                key: value for key, value in raw.items() if key in {"nse_filing", "nse_deal", "discovery_query", "timestamp_flags"}
            } | {"entity_basis": {entity.symbol: entity.basis for entity in entities}},
        )

    # ------------------------------------------------------------ events
    def build(self, story: Story, *, history: tuple[SemanticEvent, ...], now: datetime) -> CanonicalEvent:
        representative = story.representative
        ordered = sorted(story.evidence, key=lambda item: (item.public_at, item.observed_at))
        text = f"{representative.title}. {representative.summary}"
        filing_subject = (representative.raw.get("nse_filing") or {}).get("subject", "")
        classification, matches = self.classifier.classify(representative.title, f"{representative.summary} {filing_subject}")
        if classification is UNCLASSIFIED or classification.subtype == "unclassified":
            for item in ordered:
                candidate, more = self.classifier.classify(item.title, item.summary)
                if candidate.subtype != "unclassified":
                    classification, matches = candidate, more
                    break

        entity_confidence: dict[str, float] = {}
        entity_basis: dict[str, str] = {}
        for item in story.evidence:
            for symbol, value in item.entity_confidence.items():
                if value > entity_confidence.get(symbol, 0.0):
                    entity_confidence[symbol] = value
                    entity_basis[symbol] = item.raw.get("entity_basis", {}).get(symbol, "resolved")
        roundup = bool(_ROUNDUP.search(representative.title)) or (
            len(entity_confidence) > 4 and representative.quality not in (SourceQuality.PRIMARY.value,)
        )
        symbols = tuple(sorted(symbol for symbol, value in entity_confidence.items() if value >= 0.85))
        if roundup:
            symbols = tuple(sorted(symbol for symbol, value in entity_confidence.items() if value >= 0.97))

        sentence = self.extractor._sentence_around(text, text.lower().find(classification.matched_text.lower()))
        modality = representative.modality
        if story.confirmation_status in ("CONFIRMED_PRIMARY",):
            primaries = [item for item in story.evidence if item.quality in ("PRIMARY", "OFFICIAL")]
            if primaries and all(item.modality == "asserted" for item in primaries):
                modality = "asserted"
        negated = modality == "negated"
        magnitude = self.extractor._extract_magnitude(sentence) or self.extractor._extract_magnitude(text)
        effects = self.extractor._extract_effects(sentence)
        crore = _crore_value(" ".join(item.title for item in story.evidence) + " " + text)
        event_id = "ev-" + story.story_id[3:]
        semantic = SemanticEvent(
            event_id=event_id, story_id=story.story_id, event_type=classification.event_type,
            trigger=classification.matched_text or representative.title[:80], event_time=story.first_public_at,
            instruments=symbols, participants=tuple(sorted(entity_basis)), magnitude=magnitude,
            direct_effect=effects.get("direct"), indirect_effect=effects.get("indirect"),
            competitor_effect=effects.get("competitor"), supply_chain_effect=effects.get("supply_chain"),
            time_horizon=classification.horizon, novelty_status="not_assessed", surprise_status="not_assessed",
            modality=modality, negated=negated,
            extraction_confidence=round(min(0.99, max(0.2, 0.55 + 0.4 * QUALITY_CONFIDENCE.get(story.best_quality, 0.4))), 3),
            evidence=tuple(
                EvidenceSpan(item.url, item.publisher, f"{item.title}. {item.summary}"[:600], 0 if item.quality in ("PRIMARY", "OFFICIAL") else 3)
                for item in ordered[:10]
            ),
            uncertainty=(), market_mechanism=None,
        )

        # -------------------------------------------------- novelty / contradiction (vs other stories)
        other = tuple(item for item in history if item.story_id != story.story_id)
        novelty = self.novelty_engine.assess(semantic, other, as_of=now)
        cross = self.contradiction_engine.assess(semantic, other)
        semantic = replace(
            semantic, novelty_status=novelty.status, novelty_score=novelty.score, novelty_reason=novelty.reason,
            contradiction_status=cross.status, narrative_state=cross.narrative_state,
            contradiction_score=cross.similarity, contradiction_reason=cross.reason,
        )
        contradictions = list(story.contradictions)
        if cross.status == "contradicted":
            contradictions.append({
                "type": "cross_story", "between": [cross.matched_event_id, event_id], "detail": cross.reason,
                "status": "open", "resolution": None, "detected_at": now.isoformat(),
            })
        open_contradictions = [item for item in contradictions if item.get("status") == "open"]

        # -------------------------------------------------- direction
        uncertainty: list[str] = []
        if classification.direction in ("positive", "negative") and classification.direction_confidence >= 0.4:
            direction, direction_confidence, direction_basis = (
                classification.direction, classification.direction_confidence, f"classification:{classification.subtype}")
        else:
            documented = self.direction_engine.assess(semantic)
            if documented.direction in ("positive", "negative"):
                direction, direction_confidence, direction_basis = documented.direction, 0.5, f"documented:{documented.basis}"
            else:
                direction = classification.direction if classification.direction in ("mixed", "neutral") else "unknown"
                direction_confidence, direction_basis = classification.direction_confidence, "not_established"
                uncertainty.append("Documented direction is not established; only market reaction can reveal it.")
        if open_contradictions:
            direction_confidence = round(direction_confidence * 0.5, 3)
            uncertainty.append("Sources disagree; confidence reduced until the contradiction is resolved.")
        if negated or modality != "asserted":
            uncertainty.append(f"Source language is {modality}.")

        # -------------------------------------------------- materiality
        engine = self.materiality_engine.assess(semantic)
        score = max(classification.materiality, engine.score if engine.status != "unknown" else 0.0)
        reasons = [f"subtype={classification.subtype}({classification.materiality:.2f})"]
        if crore is not None:
            if crore >= 5000:
                score += 0.25
                reasons.append(f"magnitude≈₹{crore:,.0f} cr (very large)")
            elif crore >= 1000:
                score += 0.15
                reasons.append(f"magnitude≈₹{crore:,.0f} cr (large)")
            elif crore >= 100:
                score += 0.05
                reasons.append(f"magnitude≈₹{crore:,.0f} cr")
            else:
                reasons.append(f"magnitude≈₹{crore:,.1f} cr (small)")
        if classification.routine:
            score = min(score, 0.05)
            reasons.append("routine disclosure")
        if roundup:
            score = min(score, 0.2)
            reasons.append("multi-stock roundup article")
        if story.confirmation_status == "UNCONFIRMED_DISCOVERY":
            score *= 0.7
            reasons.append("discovery-only evidence")
        if novelty.status in ("repeat", "stale_repackaged"):
            score *= 0.5
            reasons.append(f"novelty={novelty.status}")
        if negated or modality != "asserted":
            score *= 0.5
            reasons.append(f"modality={modality}")
        score = round(max(0.0, min(0.99, score)), 3)
        status = "high" if score >= 0.7 else "medium" if score >= 0.45 else "low"

        # -------------------------------------------------- exposure
        direct = tuple((symbol, entity_confidence[symbol]) for symbol in symbols)
        exposures = self.graph.expand(
            ExposureRequest(
                direct_symbols=direct, event_type=classification.event_type, subtype=classification.subtype,
                direction=direction, materiality=score, text=" ".join(item.title for item in story.evidence[:5]) + " " + text,
                factor=classification.factor, factor_direction=classification.factor_direction,
            )
        ) if not (classification.routine or roundup or negated) else []
        if not symbols and not exposures:
            uncertainty.append("No configured instrument or exposure channel was resolved for this event.")

        latencies = [item.ingestion_latency_seconds for item in story.evidence if item.ingestion_latency_seconds is not None]
        semantic = replace(
            semantic, materiality_status=status, materiality_score=score, materiality_reason="; ".join(reasons),
            uncertainty=tuple(uncertainty),
        )
        return CanonicalEvent(
            event_id=event_id, story_id=story.story_id, headline=representative.title, summary=representative.summary[:600],
            event_type=classification.event_type, subtype=classification.subtype,
            secondary_subtypes=tuple(item.subtype for item in matches if item.subtype != classification.subtype)[:3],
            routine=classification.routine, roundup=roundup, symbols=symbols, entity_confidence=entity_confidence,
            entity_basis=entity_basis, direction=direction, direction_confidence=round(direction_confidence, 3),
            direction_basis=direction_basis, materiality_score=score, materiality_status=status,
            materiality_reason="; ".join(reasons), magnitude_text=magnitude.text if magnitude else None,
            magnitude_crore=crore, novelty_status=novelty.status, novelty_score=novelty.score,
            novelty_reason=novelty.reason, modality=modality, negated=negated, source_quality=story.best_quality,
            source_confidence=QUALITY_CONFIDENCE.get(story.best_quality, 0.4),
            confirmation_status=story.confirmation_status, source_count=len(story.evidence),
            publisher_count=len(story.publishers), publishers=tuple(story.publishers),
            source_urls=tuple(dict.fromkeys(item.url for item in ordered))[:10], public_at=story.first_public_at,
            public_at_basis=min(story.evidence, key=lambda item: item.public_at).public_at_basis,
            first_seen=story.first_seen, latest_seen=story.latest_seen,
            ingestion_latency_seconds=min(latencies) if latencies else None,
            session_relation=self.calendar.event_session_relation(story.first_public_at, now),
            contradiction_status="open" if open_contradictions else ("resolved" if contradictions else "none"),
            contradictions=contradictions, factor=classification.factor, factor_direction=classification.factor_direction,
            exposures=exposures, semantic_event=semantic, uncertainty=tuple(dict.fromkeys(uncertainty)),
        )


def observation_id(observation: RawObservation) -> str:
    basis = f"{observation.provider_id}|{observation.url}|{observation.title}"
    return "ob-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]


__all__ = ["CanonicalEvent", "EventBuilder", "observation_id"]
