from datetime import datetime, timezone

from psygridevents.acquisition import RawObservation
from psygridevents.news_providers import MessageNormalizer, ProviderMessage


def test_message_normalizer_preserves_provider_identity_and_timestamp():
    published = datetime(2026, 10, 4, 9, 31, tzinfo=timezone.utc)
    messages = [
        ProviderMessage(
            provider_id="lseg_reuters",
            publisher="Reuters",
            title="  Company wins major order  ",
            url="https://example.test/story",
            summary="Order details.",
            published_at=published,
            raw={"story_id": "abc"},
        )
    ]

    result = MessageNormalizer().normalize_messages(messages)

    assert len(result) == 1
    observation = result[0]
    assert isinstance(observation, RawObservation)
    assert observation.provider_id == "lseg_reuters"
    assert observation.publisher == "Reuters"
    assert observation.title == "Company wins major order"
    assert observation.published_at == published
    assert observation.raw["story_id"] == "abc"


def test_message_normalizer_rejects_empty_headlines():
    result = MessageNormalizer().normalize_messages(
        [ProviderMessage(provider_id="benzinga", publisher="Benzinga", title="   ")]
    )

    assert result == []
