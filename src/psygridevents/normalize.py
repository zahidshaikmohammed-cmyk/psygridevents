from __future__ import annotations

import hashlib
import html
import re
import unicodedata

from .acquisition import RawObservation

_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def clean_text(value: str) -> str:
    value = html.unescape(unicodedata.normalize("NFKC", value or ""))
    return _WS.sub(" ", value).strip()


def canonical_text(value: str) -> str:
    return _NON_ALNUM.sub(" ", clean_text(value).lower()).strip()


def stable_observation_id(observation: RawObservation) -> str:
    basis = "|".join(
        [
            observation.provider_id,
            canonical_text(observation.title),
            observation.url.split("#", 1)[0].rstrip("/"),
            observation.published_at.isoformat() if observation.published_at else "",
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]


def normalized_observation(observation: RawObservation) -> RawObservation:
    return RawObservation(
        provider_id=observation.provider_id,
        source_tier=observation.source_tier,
        publisher=clean_text(observation.publisher),
        title=clean_text(observation.title),
        url=observation.url.strip(),
        summary=clean_text(observation.summary),
        published_at=observation.published_at,
        observed_at=observation.observed_at,
        raw=observation.raw,
    )
