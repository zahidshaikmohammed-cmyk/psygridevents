from datetime import datetime, timedelta, timezone

import httpx

from psygridevents.health import build_health_report, health_report_to_dict
from psygridevents.market_data import NullMarketDataAdapter, PsygridMarketDataAdapter
from psygridevents.psygrid_client import PsygridClient
from psygridevents.state_store import PublicationStateStore
from psygridevents.story_engine import ProviderAcquisitionStatus

T0 = datetime(2026, 9, 19, 9, 30, tzinfo=timezone.utc)


def _psygrid_adapter(handler) -> PsygridMarketDataAdapter:
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport, base_url="http://psygrid.test")
    return PsygridMarketDataAdapter("http://psygrid.test", client=PsygridClient("http://psygrid.test", client=http_client))


def test_light_health_check_reports_universe_and_no_providers() -> None:
    report = build_health_report(market_data=NullMarketDataAdapter(), market_data_source="none")
    assert report.application_status == "OK"
    assert report.universe_status == "OK"
    assert report.universe_count == 990
    assert report.providers == ()
    assert report.market_connectivity == "N/A"


def test_health_check_reflects_market_open() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"service": "PSYGRID", "status": "OK", "stocks": {}})

    adapter = _psygrid_adapter(handler)
    report = build_health_report(market_data=adapter, market_data_source="psygrid")
    assert report.market_connectivity == "MARKET_OPEN"
    assert report.market_raw_status == "OK"
    assert report.application_status == "OK"


def test_health_check_is_degraded_when_psygrid_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = _psygrid_adapter(handler)
    report = build_health_report(market_data=adapter, market_data_source="psygrid")
    assert report.market_connectivity == "MARKET_DATA_UNAVAILABLE"
    assert report.application_status == "DEGRADED"


def test_health_check_is_degraded_on_provider_error() -> None:
    status = {
        "good": ProviderAcquisitionStatus("good", "Good Pub", T0, True, 3, None),
        "bad": ProviderAcquisitionStatus("bad", "Bad Pub", T0, False, 0, "ConnectError"),
    }
    report = build_health_report(
        market_data=NullMarketDataAdapter(), market_data_source="none", acquisition_status=status, stories=[]
    )
    assert report.application_status == "DEGRADED"
    by_id = {p.provider_id: p for p in report.providers}
    assert by_id["good"].status == "OK"
    assert by_id["bad"].status == "ERROR"
    assert by_id["bad"].last_error == "ConnectError"


def test_health_check_is_unavailable_when_universe_cannot_load(tmp_path, monkeypatch) -> None:
    import psygridevents.health as health_module

    def broken_load_instruments():
        from psygridevents.universe_integrity import CanonicalUniverseUnavailableError

        raise CanonicalUniverseUnavailableError("test-induced failure")

    monkeypatch.setattr("psygridevents.universe.load_instruments", broken_load_instruments)
    report = build_health_report(market_data=NullMarketDataAdapter(), market_data_source="none")
    assert report.universe_status == "CANONICAL_UNIVERSE_UNAVAILABLE"
    assert report.application_status == "UNAVAILABLE"
    assert "test-induced failure" in report.universe_error


def test_provider_status_persists_across_restart_when_not_attempted_this_run(tmp_path) -> None:
    store = PublicationStateStore(tmp_path / "state.json")
    store.record_provider_attempt("sebi_rss", success=True, at=T0)
    store.save()

    reloaded = PublicationStateStore(tmp_path / "state.json")
    report = build_health_report(
        market_data=NullMarketDataAdapter(), market_data_source="none",
        acquisition_status={}, stories=[], store=reloaded,
    )
    assert len(report.providers) == 1
    assert report.providers[0].provider_id == "sebi_rss"
    assert report.providers[0].status == "NOT_ATTEMPTED"
    assert report.providers[0].last_success_at == T0


def test_provider_failure_this_run_falls_back_to_persisted_last_success(tmp_path) -> None:
    store = PublicationStateStore(tmp_path / "state.json")
    store.record_provider_attempt("sebi_rss", success=True, at=T0)

    status = {"sebi_rss": ProviderAcquisitionStatus("sebi_rss", "SEBI", T0 + timedelta(hours=2), False, 0, "timeout")}
    report = build_health_report(
        market_data=NullMarketDataAdapter(), market_data_source="none",
        acquisition_status=status, stories=[], store=store,
    )
    by_id = {p.provider_id: p for p in report.providers}
    assert by_id["sebi_rss"].status == "ERROR"
    assert by_id["sebi_rss"].last_success_at == T0  # preserved from before this run's failure


def test_health_report_to_dict_is_json_serializable() -> None:
    import json

    report = build_health_report(market_data=NullMarketDataAdapter(), market_data_source="none")
    payload = health_report_to_dict(report)
    json.dumps(payload)  # must not raise
    assert payload["universe"]["count"] == 990
