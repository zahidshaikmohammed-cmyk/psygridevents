from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import httpx
import pytest

from psygridevents.acquisition import RawObservation
from psygridevents.story_engine import StoryEngine

ROOT = Path(__file__).resolve().parents[1]
INSTRUMENTS = ROOT / "config" / "instruments.json"

FEEDS = [
    {"provider_id": "good_feed", "publisher": "Good Publisher", "url": "https://good.example/rss", "tier": 0, "enabled": True},
    {"provider_id": "bad_feed", "publisher": "Bad Publisher", "url": "https://bad.example/rss", "tier": 0, "enabled": True},
]


def _observation(provider_id: str) -> RawObservation:
    now = datetime(2026, 9, 20, 9, 0, tzinfo=timezone.utc)
    return RawObservation(
        provider_id=provider_id, source_tier=0, publisher="Good Publisher",
        title=f"{provider_id} headline", url=f"https://good.example/{provider_id}",
        summary="", published_at=now, observed_at=now, raw={},
    )


def _fetch_one_good_one_broken(self, *, provider_id, publisher, url, source_tier, since=None):
    if provider_id == "bad_feed":
        raise httpx.ConnectError("connection refused")
    return [_observation(provider_id)]


def test_one_feed_failure_does_not_prevent_the_other_feed_from_being_acquired() -> None:
    engine = StoryEngine(INSTRUMENTS)
    with mock.patch("psygridevents.acquisition.RSSAcquirer.fetch", _fetch_one_good_one_broken):
        observations = engine.acquire(FEEDS)

    assert len(observations) == 1
    assert observations[0].provider_id == "good_feed"


def test_provider_acquisition_status_records_both_success_and_failure() -> None:
    engine = StoryEngine(INSTRUMENTS)
    with mock.patch("psygridevents.acquisition.RSSAcquirer.fetch", _fetch_one_good_one_broken):
        engine.acquire(FEEDS)

    status = engine.last_acquisition_status
    assert status["good_feed"].success is True
    assert status["good_feed"].observation_count == 1
    assert status["good_feed"].error is None
    assert status["bad_feed"].success is False
    assert status["bad_feed"].observation_count == 0
    assert "ConnectError" in status["bad_feed"].error


def test_disabled_and_catalogue_feeds_are_skipped_and_not_reported_as_failed() -> None:
    engine = StoryEngine(INSTRUMENTS)
    feeds = [
        {"provider_id": "disabled_feed", "publisher": "X", "url": "https://x.example/rss", "tier": 0, "enabled": False},
        {"provider_id": "catalogue_feed", "publisher": "Y", "url": "https://y.example/rss", "tier": 0, "enabled": False, "mode": "catalogue"},
    ]
    with mock.patch("psygridevents.acquisition.RSSAcquirer.fetch", _fetch_one_good_one_broken):
        observations = engine.acquire(feeds)

    assert observations == []
    assert engine.last_acquisition_status == {}


def test_acquisition_status_starts_empty_before_any_acquire_call() -> None:
    engine = StoryEngine(INSTRUMENTS)
    assert engine.last_acquisition_status == {}


@pytest.mark.parametrize(
    "exc",
    [
        httpx.TimeoutException("timed out"),
        httpx.ConnectError("connection refused"),
        httpx.HTTPStatusError("503", request=None, response=None),
        ValueError("malformed feed content"),
    ],
)
def test_every_kind_of_provider_failure_is_isolated(exc) -> None:
    def fetch(self, *, provider_id, publisher, url, source_tier, since=None):
        if provider_id == "bad_feed":
            raise exc
        return [_observation(provider_id)]

    engine = StoryEngine(INSTRUMENTS)
    with mock.patch("psygridevents.acquisition.RSSAcquirer.fetch", fetch):
        observations = engine.acquire(FEEDS)

    assert len(observations) == 1
    assert engine.last_acquisition_status["bad_feed"].success is False
