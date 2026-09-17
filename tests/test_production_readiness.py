from datetime import datetime, timezone
from pathlib import Path

from psygridevents.acquisition import RawObservation
from psygridevents.catalogue import extract_feed_links, validate_rss_url
from psygridevents.clustering import StoryCluster
from psygridevents.entity_resolution import EntityMatch
from psygridevents.evidence import assess_evidence
from psygridevents.priority import PriorityEngine
from psygridevents.semantic import SemanticExtractor
from psygridevents.story_engine import StoryEngine, StoryIntelligence
from psygridevents.transmission import TransmissionEngine

ROOT = Path(__file__).resolve().parents[1]


def intelligence(title: str, *, url: str = "https://example.com/test") -> StoryIntelligence:
    now = datetime(2026, 9, 17, 8, tzinfo=timezone.utc)
    observation = RawObservation(
        provider_id="test",
        source_tier=0,
        publisher="Official Test Source",
        title=title,
        url=url,
        summary=title,
        published_at=now,
        observed_at=now,
        raw={},
    )
    story = StoryCluster("story-test", (observation,), observation, "test")
    entity = EntityMatch("RELIANCE", "reliance", 0, 8, 0.99)
    return StoryIntelligence(story, (entity,), assess_evidence([observation]))


def extract(title: str):
    return SemanticExtractor(ROOT / "config" / "semantic_rules.yaml").extract(intelligence(title))[0]


def test_certificate_no_identifier_is_not_negated():
    event = extract("SEBI compliance order Certificate No. 5742")
    assert event.negated is False
    assert event.modality == "asserted"


def test_order_no_identifier_is_not_negated():
    event = extract("Company order awarded, Order No. 123")
    assert event.negated is False


def test_no_acquisition_is_planned_is_negated():
    event = extract("No acquisition is planned")
    assert event.negated is True
    assert event.modality == "negated"


def test_did_not_win_order_is_negated():
    event = extract("The company did not win the contract award")
    assert event.negated is True
    assert event.modality == "negated"


def test_regulator_denied_is_negated():
    event = extract("The regulator denied the allegation")
    assert event.negated is True
    assert event.modality == "negated"


def test_has_no_plans_is_negated():
    event = extract("The company has no plans to acquire the target")
    assert event.negated is True
    assert event.modality == "negated"


def test_no_approval_is_negated():
    event = extract("There was no compliance order approval for the transaction")
    assert event.negated is True
    assert event.modality == "negated"


def test_negated_event_cannot_receive_actionable_priority():
    from dataclasses import replace

    event = extract("SEBI compliance order Certificate No. 5742")
    event = replace(event, negated=True, modality="negated")
    assessment = PriorityEngine(ROOT / "config" / "priority_rules.yaml").assess(event, intelligence("x").evidence)
    assert assessment.priority_class == "informational"
    assert assessment.priority_score == 0.0
    assert assessment.component_scores == {}


def test_asserted_event_keeps_cp6_score_behavior():
    from dataclasses import replace

    event = extract("RELIANCE wins order worth INR 500 crore")
    event = replace(event, materiality_status="high", materiality_score=0.9, novelty_status="new", novelty_score=0.9, time_horizon="near_term")
    transmission = TransmissionEngine(ROOT / "config" / "exposure_rules.yaml").assess(event)
    assessment = PriorityEngine(ROOT / "config" / "priority_rules.yaml").assess(event, intelligence("x").evidence, transmission)
    assert assessment.priority_score == 90.2
    assert assessment.priority_class == "high"
    assert assessment.coverage == 0.82


def test_clustering_reuses_tokens_without_changing_grouping(monkeypatch):
    from psygridevents import clustering

    now = datetime(2026, 9, 17, 8, tzinfo=timezone.utc)
    observations = [
        RawObservation("test", 0, "Source A", "RBI cuts repo rate", "https://example.com/a", "RBI cuts repo rate", now, now, {}),
        RawObservation("test", 0, "Source B", "RBI cuts policy repo rate", "https://example.com/b", "RBI cuts policy repo rate", now, now, {}),
        RawObservation("test", 0, "Source C", "Company launches new product", "https://example.com/c", "Company launches new product", now, now, {}),
    ]
    calls = 0
    original = clustering.canonical_text

    def counted(value: str) -> str:
        nonlocal calls
        calls += 1
        return original(value)

    monkeypatch.setattr(clustering, "canonical_text", counted)
    clusters = clustering.cluster_stories(observations)

    assert clustering.cluster_stories.__defaults__ == (0.42,)
    assert [[item.url for item in cluster.observations] for cluster in clusters] == [
        ["https://example.com/a", "https://example.com/b"],
        ["https://example.com/c"],
    ]
    assert calls == len(observations)


def test_official_catalogue_extracts_only_allowed_rss_links():
    html = '<a href="/feeds/a_rss.xml">A</a><a href="https://example.com/b.xml">B</a><a href="/page">Page</a>'
    assert extract_feed_links("https://official.example/catalogue", html, allowed_hosts={"official.example"}) == ("https://official.example/feeds/a_rss.xml",)


def test_undocumented_host_is_rejected():
    html = '<a href="https://unknown.example/feed.xml">RSS</a>'
    assert extract_feed_links("https://official.example/catalogue", html, allowed_hosts={"official.example"}) == ()


def test_invalid_feed_url_fails_closed():
    assert validate_rss_url("https://example.invalid/not-a-feed.xml") is False


def test_rbi_verified_feed_urls_are_catalogued():
    text = (ROOT / "config" / "feed_endpoints.yaml").read_text(encoding="utf-8")
    for suffix in ("pressreleases_rss.xml", "notifications_rss.xml", "speeches_rss.xml", "Publication_rss.xml"):
        assert suffix in text


def test_first_history_run_is_empty_and_unassessed(tmp_path):
    history = tmp_path / "events.json"
    engine = StoryEngine(ROOT / "config" / "instruments.json")
    assert engine.load_history(history) == ()
    raw = RawObservation(
        provider_id="test",
        source_tier=0,
        publisher="Official Test Source",
        title="RELIANCE wins order",
        url="https://example.com/history-test",
        summary="RELIANCE wins order",
        published_at=datetime(2026, 9, 17, 8, tzinfo=timezone.utc),
        observed_at=datetime(2026, 9, 17, 8, 1, tzinfo=timezone.utc),
        raw={},
    )
    stories = engine.build_stories([raw], engine.load_history(history))
    event = stories[0].semantic_events[0]
    assert event.novelty_status == "not_assessed"
    assert event.contradiction_status == "not_assessed"


def test_no_market_observations_are_untested():
    engine = StoryEngine(ROOT / "config" / "instruments.json")
    item = intelligence("RELIANCE wins order")
    event = SemanticExtractor(ROOT / "config" / "semantic_rules.yaml").extract(item)[0]
    item = StoryIntelligence(item.story, item.entities, item.evidence, (event,))
    item = engine.build_market_confirmation([item], [])[0]
    assert item.semantic_events[0].market_confirmation_status == "untested"
