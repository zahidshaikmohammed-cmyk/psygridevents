from datetime import datetime, timezone
from pathlib import Path

import pytest

from psygridevents.catalogue import CatalogueResolutionError, extract_feed_links, validate_rss_url
from psygridevents.clustering import StoryCluster
from psygridevents.acquisition import RawObservation
from psygridevents.entity_resolution import EntityMatch
from psygridevents.evidence import assess_evidence
from psygridevents.priority import PriorityEngine
from psygridevents.semantic import SemanticExtractor
from psygridevents.story_engine import StoryEngine, StoryIntelligence
from psygridevents.transmission import TransmissionEngine

ROOT = Path(__file__).resolve().parents[1]


def intelligence(title: str) -> StoryIntelligence:
    now = datetime(2026, 9, 17, 8, tzinfo=timezone.utc)
    observation = RawObservation(
        provider_id="test",
        source_tier=0,
        publisher="Official Test Source",
        title=title,
        url="https://example.com/test",
        summary=title,
        published_at=now,
        observed_at=now,
        raw={},
    )
    story = StoryCluster("story-test", (observation,), observation, "test")
    entity = EntityMatch("RELIANCE", "reliance", 0, 8, 0.99)
    return StoryIntelligence(story, (entity,), assess_evidence([observation]))


def test_certificate_no_identifier_is_not_negated():
    event = SemanticExtractor(ROOT / "config" / "semantic_rules.yaml").extract(intelligence("SEBI compliance order Certificate No. 5742"))[0]
    assert event.negated is False
    assert event.modality == "asserted"


def test_order_no_identifier_is_not_negated():
    event = SemanticExtractor(ROOT / "config" / "semantic_rules.yaml").extract(intelligence("Company order awarded, Order No. 123"))[0]
    assert event.negated is False


@pytest.mark.parametrize(
    "title",
    [
        "No acquisition is planned",
        "The company did not win the order",
        "The regulator denied the allegation",
        "The company has no plans to acquire the target",
        "There was no approval for the transaction",
    ],
)
def test_real_negation_remains_non_asserted(title: str):
    event = SemanticExtractor(ROOT / "config" / "semantic_rules.yaml").extract(intelligence(title))[0]
    assert event.negated is True
    assert event.modality == "negated"


def test_negated_event_cannot_receive_actionable_priority():
    event = SemanticExtractor(ROOT / "config" / "semantic_rules.yaml").extract(intelligence("SEBI compliance order Certificate No. 5742"))[0]
    from dataclasses import replace
    event = replace(event, negated=True, modality="negated")
    assessment = PriorityEngine(ROOT / "config" / "priority_rules.yaml").assess(event, intelligence("x").evidence)
    assert assessment.priority_class == "informational"
    assert assessment.priority_score == 0.0
    assert assessment.component_scores == {}


def test_valid_asserted_event_keeps_cp6_formula_behavior():
    event = SemanticExtractor(ROOT / "config" / "semantic_rules.yaml").extract(intelligence("RELIANCE wins order worth INR 500 crore"))[0]
    from dataclasses import replace
    event = replace(event, materiality_status="high", materiality_score=0.9, novelty_status="new", novelty_score=0.9, time_horizon="near_term")
    transmission = TransmissionEngine(ROOT / "config" / "exposure_rules.yaml").assess(event)
    assessment = PriorityEngine(ROOT / "config" / "priority_rules.yaml").assess(event, intelligence("x").evidence, transmission)
    assert assessment.priority_class in {"medium", "high", "critical"}
    assert assessment.component_scores["source_confidence"] == 1.0


def test_official_catalogue_extracts_only_official_rss_links():
    html = '<a href="/feeds/a_rss.xml">A</a><a href="https://example.com/b.xml">B</a><a href="/page">Page</a>'
    assert extract_feed_links("https://official.example/catalogue", html, allowed_hosts={"official.example"}) == ("https://official.example/feeds/a_rss.xml",)


def test_guessed_or_external_feed_url_is_rejected():
    html = '<a href="https://nsearchives.nseindia.com/content/rss/guessed.xml">RSS</a>'
    assert extract_feed_links("https://www.nseindia.com/static/rss-feed", html, allowed_hosts={"www.nseindia.com"}) == ()


def test_feed_validation_fails_closed_for_invalid_url():
    assert validate_rss_url("https://example.invalid/not-a-feed") is False


def test_rbi_verified_feed_urls_are_catalogued():
    text = (ROOT / "config" / "feed_endpoints.yaml").read_text(encoding="utf-8")
    assert "pressreleases_rss.xml" in text
    assert "notifications_rss.xml" in text
    assert "speeches_rss.xml" in text
    assert "Publication_rss.xml" in text


def test_first_history_run_stays_unassessed(tmp_path):
    history = tmp_path / "events.json"
    assert StoryEngine.load_history(history) == ()


def test_no_market_observations_are_untested():
    engine = StoryEngine(ROOT / "config" / "instruments.json")
    item = intelligence("RELIANCE wins order")
    item = engine.build_market_confirmation([item], [])[0]
    assert item.semantic_events[0].market_confirmation_status == "untested"
