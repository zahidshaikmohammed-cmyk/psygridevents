import json
from datetime import datetime, timedelta, timezone

from psygridevents.state_store import PublicationStateStore

T0 = datetime(2026, 9, 20, 9, 30, tzinfo=timezone.utc)


def test_fresh_store_has_no_since_and_no_signals(tmp_path) -> None:
    store = PublicationStateStore(tmp_path / "state.json")
    assert store.since() is None
    assert store.last_signal_state("event-1") is None
    assert store.known_event_ids() == frozenset()


def test_record_and_reload_signal_state_survives_restart(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = PublicationStateStore(path)
    store.record_signal("event-1", "EARLY_LONG", as_of=T0)
    store.advance_since(T0)
    store.save()

    restarted = PublicationStateStore(path)  # simulates a process restart
    assert restarted.last_signal_state("event-1") == "EARLY_LONG"
    assert restarted.since() == T0
    assert restarted.known_event_ids() == frozenset({"event-1"})


def test_recording_the_same_state_twice_is_idempotent(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = PublicationStateStore(path)
    store.record_signal("event-1", "WATCH", as_of=T0)
    store.save()
    first_write = path.read_text(encoding="utf-8")

    store.record_signal("event-1", "WATCH", as_of=T0)
    store.save()
    second_write = path.read_text(encoding="utf-8")

    assert json.loads(first_write) == json.loads(second_write)


def test_advance_since_never_moves_backward(tmp_path) -> None:
    store = PublicationStateStore(tmp_path / "state.json")
    store.advance_since(T0)
    store.advance_since(T0 - timedelta(hours=1))  # an out-of-order/older as_of
    assert store.since() == T0


def test_advance_since_moves_forward(tmp_path) -> None:
    store = PublicationStateStore(tmp_path / "state.json")
    store.advance_since(T0)
    store.advance_since(T0 + timedelta(minutes=5))
    assert store.since() == T0 + timedelta(minutes=5)


def test_corrupted_state_file_is_treated_as_empty_not_a_crash(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = PublicationStateStore(path)
    assert store.since() is None
    assert store.last_signal_state("event-1") is None


def test_malformed_state_shape_is_treated_as_empty(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    store = PublicationStateStore(path)
    assert store.since() is None


def test_save_is_atomic_no_leftover_temp_file(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = PublicationStateStore(path)
    store.record_signal("event-1", "CONFIRMED", as_of=T0)
    store.save()

    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["signals"]["event-1"]["signal_state"] == "CONFIRMED"


def test_provider_success_records_last_success_and_clears_error(tmp_path) -> None:
    store = PublicationStateStore(tmp_path / "state.json")
    store.record_provider_attempt("sebi_rss", success=True, at=T0)
    status = store.provider_status("sebi_rss")
    assert status["last_success_at"] == T0.isoformat()
    assert status["last_error"] is None


def test_provider_failure_preserves_last_known_success(tmp_path) -> None:
    store = PublicationStateStore(tmp_path / "state.json")
    store.record_provider_attempt("sebi_rss", success=True, at=T0)
    store.record_provider_attempt("sebi_rss", success=False, at=T0 + timedelta(hours=1), error="ConnectError: refused")

    status = store.provider_status("sebi_rss")
    assert status["last_success_at"] == T0.isoformat()  # not erased by the later failure
    assert status["last_attempt_at"] == (T0 + timedelta(hours=1)).isoformat()
    assert "ConnectError" in status["last_error"]


def test_unknown_provider_status_is_none(tmp_path) -> None:
    store = PublicationStateStore(tmp_path / "state.json")
    assert store.provider_status("never_seen") is None


def test_provider_status_survives_restart(tmp_path) -> None:
    path = tmp_path / "state.json"
    store = PublicationStateStore(path)
    store.record_provider_attempt("pib_rss", success=True, at=T0)
    store.save()

    restarted = PublicationStateStore(path)
    assert restarted.provider_status("pib_rss")["last_success_at"] == T0.isoformat()


def test_multiple_events_tracked_independently(tmp_path) -> None:
    store = PublicationStateStore(tmp_path / "state.json")
    store.record_signal("event-1", "WATCH", as_of=T0)
    store.record_signal("event-2", "EARLY_SHORT", as_of=T0)
    store.save()

    reloaded = PublicationStateStore(tmp_path / "state.json")
    assert reloaded.last_signal_state("event-1") == "WATCH"
    assert reloaded.last_signal_state("event-2") == "EARLY_SHORT"
    assert reloaded.known_event_ids() == frozenset({"event-1", "event-2"})
