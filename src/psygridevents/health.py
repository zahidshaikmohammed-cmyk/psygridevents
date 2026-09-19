from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .market_data import MarketDataAdapter, PsygridMarketDataAdapter
from .state_store import PublicationStateStore
from .story_engine import ProviderAcquisitionStatus, StoryIntelligence
from .universe_integrity import CanonicalUniverseUnavailableError, check_universe_integrity

APPLICATION_OK = "OK"
APPLICATION_DEGRADED = "DEGRADED"
APPLICATION_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class ProviderHealth:
    provider_id: str
    publisher: str
    status: str  # "OK" | "ERROR" | "NOT_ATTEMPTED"
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_error: str | None
    observation_count: int


@dataclass(frozen=True)
class PipelineCounts:
    """Diagnostic-only counters. Never used to decide a signal."""

    raw_observations: int = 0
    stories: int = 0
    semantic_events: int = 0
    entity_resolved: int = 0
    materiality_assessed: int = 0
    asset_resolved: int = 0
    market_tested: int = 0
    untested: int = 0
    signals_by_state: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class HealthReport:
    application_status: str  # OK | DEGRADED | UNAVAILABLE
    generated_at: datetime
    universe_status: str  # "OK" | "CANONICAL_UNIVERSE_UNAVAILABLE"
    universe_count: int
    universe_error: str | None
    providers: tuple[ProviderHealth, ...]
    market_data_source: str  # "psygrid" | "none"
    market_connectivity: str  # MARKET_OPEN | MARKET_CLOSED | MARKET_DATA_UNAVAILABLE | "N/A"
    market_raw_status: str | None
    pipeline: PipelineCounts
    pipeline_latency_seconds: float | None


def _universe_health() -> tuple[str, int, str | None]:
    from .universe import load_instruments  # local import: avoids import cycle at module load

    try:
        instruments = load_instruments()
    except CanonicalUniverseUnavailableError as exc:
        return "CANONICAL_UNIVERSE_UNAVAILABLE", 0, str(exc)
    report = check_universe_integrity(instruments)
    if not report.is_valid:
        return "CANONICAL_UNIVERSE_UNAVAILABLE", report.count, "; ".join(report.reasons)
    return "OK", report.count, None


def _provider_health(
    acquisition_status: dict[str, ProviderAcquisitionStatus],
    store: PublicationStateStore | None,
) -> tuple[ProviderHealth, ...]:
    providers: list[ProviderHealth] = []
    provider_ids = set(acquisition_status)
    if store is not None:
        provider_ids |= set(store.known_provider_ids())
    for provider_id in sorted(provider_ids):
        current = acquisition_status.get(provider_id)
        persisted = store.provider_status(provider_id) if store is not None else None
        if current is not None:
            last_success_at = current.attempted_at if current.success else (
                datetime.fromisoformat(persisted["last_success_at"])
                if persisted and persisted.get("last_success_at") else None
            )
            providers.append(
                ProviderHealth(
                    provider_id=provider_id,
                    publisher=current.publisher,
                    status="OK" if current.success else "ERROR",
                    last_attempt_at=current.attempted_at,
                    last_success_at=last_success_at,
                    last_error=current.error,
                    observation_count=current.observation_count,
                )
            )
        elif persisted is not None:
            providers.append(
                ProviderHealth(
                    provider_id=provider_id,
                    publisher="",
                    status="NOT_ATTEMPTED",
                    last_attempt_at=datetime.fromisoformat(persisted["last_attempt_at"]) if persisted.get("last_attempt_at") else None,
                    last_success_at=datetime.fromisoformat(persisted["last_success_at"]) if persisted.get("last_success_at") else None,
                    last_error=persisted.get("last_error"),
                    observation_count=0,
                )
            )
    return tuple(providers)


def _pipeline_counts(stories: list[StoryIntelligence]) -> PipelineCounts:
    raw_observations = sum(len(item.story.observations) for item in stories)
    semantic_events = sum(len(item.semantic_events) for item in stories)
    entity_resolved = sum(1 for item in stories for event in item.semantic_events if event.instruments)
    materiality_assessed = sum(
        1 for item in stories for event in item.semantic_events
        if event.materiality_status not in ("", "not_assessed")
    )
    asset_resolved = sum(
        1
        for item in stories
        for mappings in item.asset_mechanisms
        for mapping in mappings
        if mapping.resolved
    )
    market_tested = sum(
        1 for item in stories for confirmation in item.market_confirmations
        if confirmation.status != "untested"
    )
    untested = sum(
        1 for item in stories for confirmation in item.market_confirmations
        if confirmation.status == "untested"
    )
    signals_by_state = Counter(signal.signal_state for item in stories for signal in item.signals)
    return PipelineCounts(
        raw_observations=raw_observations,
        stories=len(stories),
        semantic_events=semantic_events,
        entity_resolved=entity_resolved,
        materiality_assessed=materiality_assessed,
        asset_resolved=asset_resolved,
        market_tested=market_tested,
        untested=untested,
        signals_by_state=dict(signals_by_state),
    )


def build_health_report(
    *,
    market_data: MarketDataAdapter,
    market_data_source: str,
    acquisition_status: dict[str, ProviderAcquisitionStatus] | None = None,
    stories: list[StoryIntelligence] | None = None,
    store: PublicationStateStore | None = None,
    pipeline_latency_seconds: float | None = None,
) -> HealthReport:
    """Diagnostic snapshot only -- never fed back into signal decisions.

    Works in two modes: a fast, pipeline-independent check (acquisition_status
    and stories both None -- suitable for a frequent uptime probe) or a full
    report attached to an actual pipeline run (both supplied).
    """
    universe_status, universe_count, universe_error = _universe_health()

    providers = _provider_health(acquisition_status or {}, store)

    market_connectivity = "N/A"
    market_raw_status = None
    if isinstance(market_data, PsygridMarketDataAdapter):
        status = market_data.market_session_status()
        market_connectivity = status.market_state
        market_raw_status = status.raw_status

    pipeline = _pipeline_counts(stories) if stories is not None else PipelineCounts()

    application_status = APPLICATION_OK
    if universe_status != "OK":
        application_status = APPLICATION_UNAVAILABLE
    elif any(p.status == "ERROR" for p in providers) or market_connectivity == "MARKET_DATA_UNAVAILABLE":
        application_status = APPLICATION_DEGRADED

    return HealthReport(
        application_status=application_status,
        generated_at=datetime.now(timezone.utc),
        universe_status=universe_status,
        universe_count=universe_count,
        universe_error=universe_error,
        providers=providers,
        market_data_source=market_data_source,
        market_connectivity=market_connectivity,
        market_raw_status=market_raw_status,
        pipeline=pipeline,
        pipeline_latency_seconds=pipeline_latency_seconds,
    )


def health_report_to_dict(report: HealthReport) -> dict:
    def _dt(value: datetime | None) -> str | None:
        return value.isoformat() if value else None

    return {
        "application_status": report.application_status,
        "generated_at": report.generated_at.isoformat(),
        "universe": {
            "status": report.universe_status,
            "count": report.universe_count,
            "error": report.universe_error,
        },
        "providers": [
            {
                "provider_id": p.provider_id,
                "publisher": p.publisher,
                "status": p.status,
                "last_attempt_at": _dt(p.last_attempt_at),
                "last_success_at": _dt(p.last_success_at),
                "last_error": p.last_error,
                "observation_count": p.observation_count,
            }
            for p in report.providers
        ],
        "market_data": {
            "source": report.market_data_source,
            "connectivity": report.market_connectivity,
            "raw_status": report.market_raw_status,
        },
        "pipeline": {
            "raw_observations": report.pipeline.raw_observations,
            "stories": report.pipeline.stories,
            "semantic_events": report.pipeline.semantic_events,
            "entity_resolved": report.pipeline.entity_resolved,
            "materiality_assessed": report.pipeline.materiality_assessed,
            "asset_resolved": report.pipeline.asset_resolved,
            "market_tested": report.pipeline.market_tested,
            "untested": report.pipeline.untested,
            "signals_by_state": report.pipeline.signals_by_state,
            "latency_seconds": report.pipeline_latency_seconds,
        },
    }
