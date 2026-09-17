from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventStatus(StrEnum):
    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"
    DENIED = "denied"
    DEVELOPING = "developing"


class EventType(StrEnum):
    CORPORATE = "corporate"
    EARNINGS = "earnings"
    ORDER = "order"
    REGULATORY = "regulatory"
    GOVERNMENT_POLICY = "government_policy"
    MACRO = "macro"
    CENTRAL_BANK = "central_bank"
    COMMODITY = "commodity"
    GEOPOLITICAL = "geopolitical"
    LEGAL = "legal"
    RATING = "rating"
    M_AND_A = "m_and_a"
    CAPITAL_ACTION = "capital_action"
    MANAGEMENT = "management"
    OPERATIONAL = "operational"
    OTHER = "other"


class Direction(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"
    NEUTRAL = "neutral"
    UNKNOWN = "unknown"


class ImpactLevel(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFORMATIONAL = "informational"
    UNKNOWN = "unknown"


class ExposureType(StrEnum):
    DIRECT = "direct"
    SECTOR = "sector"
    COMPETITIVE = "competitive"
    SUPPLY_CHAIN = "supply_chain"
    COMMODITY = "commodity"
    MACRO = "macro"
    SECOND_ORDER = "second_order"


class TimeHorizon(StrEnum):
    IMMEDIATE = "immediate"
    INTRADAY = "intraday"
    MULTI_DAY = "multi_day"
    LONG_TERM = "long_term"
    UNKNOWN = "unknown"


class SourceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    publisher: str
    url: str
    published_at: datetime | None = None
    source_tier: int = Field(default=3, ge=1, le=5)
    title: str
    excerpt: str | None = None


class Event(BaseModel):
    """Canonical event object. Facts are kept separate from derived intelligence."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    observed_at: datetime
    event_time: datetime | None = None
    event_type: EventType
    status: EventStatus
    headline: str
    factual_summary: str
    entities: list[str] = Field(default_factory=list)
    instruments: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)
    sources: list[SourceEvidence] = Field(default_factory=list)
    first_seen_at: datetime | None = None
    is_new_information: bool = True
    raw_payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("instruments", "entities", "sectors")
    @classmethod
    def unique_values(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(x.strip() for x in value if x and x.strip()))


class ExposureAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instrument: str
    exposure_type: ExposureType
    direction: Direction
    impact_level: ImpactLevel
    mechanism: str
    time_horizon: TimeHorizon = TimeHorizon.UNKNOWN
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class IntelligenceAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    materiality: ImpactLevel
    priority_score: float = Field(ge=0.0, le=100.0)
    novelty_score: float = Field(ge=0.0, le=1.0)
    surprise_score: float = Field(ge=0.0, le=1.0)
    source_confidence: float = Field(ge=0.0, le=1.0)
    market_relevance: float = Field(ge=0.0, le=1.0)
    exposures: list[ExposureAssessment] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    interpretation: str | None = None
    market_implication: str | None = None
    uncertainty: list[str] = Field(default_factory=list)


class IntelligenceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Event
    assessment: IntelligenceAssessment
