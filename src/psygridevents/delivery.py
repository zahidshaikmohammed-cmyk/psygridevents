from __future__ import annotations

from datetime import date, datetime
from typing import Any, Iterable

from .priority import PriorityAssessment
from .story_engine import StoryIntelligence

SCHEMA_VERSION = "1.1"


def _jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "__dataclass_fields__"):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    return value


def _source(observation: Any) -> dict[str, Any]:
    return {
        "provider_id": observation.provider_id,
        "source_tier": observation.source_tier,
        "publisher": observation.publisher,
        "title": observation.title,
        "url": observation.url,
        "published_at": _jsonable(observation.published_at),
        "observed_at": _jsonable(observation.observed_at),
    }


def _story(item: StoryIntelligence) -> dict[str, Any]:
    return {
        "story_id": item.story.cluster_id,
        "representative": _source(item.story.representative),
        "sources": [_source(observation) for observation in item.story.observations],
        "entities": _jsonable(item.entities),
        "evidence": _jsonable(item.evidence),
        "events": [_jsonable(event) for event in item.semantic_events],
        "transmissions": _jsonable(item.transmissions),
        "contradictions": _jsonable(item.contradictions),
        "market_confirmations": _jsonable(item.market_confirmations),
        "priorities": _jsonable(item.priorities),
        "asset_mechanisms": _jsonable(item.asset_mechanisms),
        "event_timings": _jsonable(item.event_timings),
        "market_responses": _jsonable(item.market_responses),
        "exhaustions": _jsonable(item.exhaustions),
        "signals": _jsonable(item.signals),
    }


def build_intelligence_payload(
    intelligence: Iterable[StoryIntelligence],
    ranked: Iterable[PriorityAssessment],
    *,
    generated_at: datetime,
) -> dict[str, Any]:
    """Build the stable CP7 machine-readable delivery contract.

    Ranked entries contain the event plus its explainable priority assessment so
    consumers do not need to perform an implicit join across payload sections.
    Raw provider payloads are deliberately excluded.

    Each ranked entry also carries its CP11 `signal` (when one was computed)
    so a downstream consumer never has to re-derive the event-driven signal
    state from the story-level detail. `signal_strength_or_confidence` is
    intentionally independent of `priority.priority_score`: priority answers
    "how important is this event", the signal answers "is there currently a
    sufficiently supported early market opportunity".
    """
    stories = tuple(intelligence)
    ranked_items = tuple(ranked)
    event_by_id = {
        event.event_id: (item, event)
        for item in stories
        for event in item.semantic_events
    }
    signal_by_event_id = {
        signal.event_id: signal
        for item in stories
        for signal in item.signals
    }

    ranked_events: list[dict[str, Any]] = []
    for priority in ranked_items:
        context = event_by_id.get(priority.event_id)
        entry: dict[str, Any] = {"priority": _jsonable(priority)}
        entry["signal"] = _jsonable(signal_by_event_id.get(priority.event_id))
        if context is None:
            entry["event"] = None
            entry["story_id"] = None
        else:
            item, event = context
            entry["story_id"] = item.story.cluster_id
            entry["event"] = _jsonable(event)
        ranked_events.append(entry)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _jsonable(generated_at),
        "engine": "psygridevents",
        "stories": [_story(item) for item in stories],
        "ranked_events": ranked_events,
        "summary": {
            "story_count": len(stories),
            "event_count": sum(len(item.semantic_events) for item in stories),
            "ranked_event_count": len(ranked_items),
            "signal_count": sum(len(item.signals) for item in stories),
        },
    }
