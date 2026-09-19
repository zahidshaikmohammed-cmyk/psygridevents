import json
from pathlib import Path

import pytest

from psygridevents.entity_resolution import InstrumentResolver
from psygridevents.universe import load_instruments
from psygridevents.universe_integrity import (
    EXPECTED_UNIVERSE_SIZE,
    CanonicalUniverseUnavailableError,
    assert_universe_integrity,
    check_universe_integrity,
)

ROOT = Path(__file__).resolve().parents[1]
INSTRUMENTS_PATH = ROOT / "config" / "instruments.json"
PSYGRID_REFERENCE_PATH = ROOT / "tests" / "fixtures" / "psygrid_stocks_reference.json"


def _psygrid_reference_symbols() -> list[str]:
    """A frozen, point-in-time copy of Psygrid's stocks.json.

    Psygrid (zahidshaikmohammed-cmyk/Psygrid) remains the single canonical
    owner of the live universe; this fixture only exists so the offline,
    network-free test suite can deterministically assert set equality
    against a known-good snapshot instead of depending on live GitHub
    access during every test run. `tools/verify_universe_against_psygrid.py`
    is the tool that checks equality against the *actual live* repository.
    """
    payload = json.loads(PSYGRID_REFERENCE_PATH.read_text(encoding="utf-8"))
    return [str(symbol).strip().upper() for symbol in payload["symbols"]]


def test_frozen_psygrid_reference_is_exactly_990_and_unique() -> None:
    symbols = _psygrid_reference_symbols()
    assert len(symbols) == EXPECTED_UNIVERSE_SIZE
    assert len(set(symbols)) == EXPECTED_UNIVERSE_SIZE


def test_psygrid_universe_loads_and_has_exactly_990_instruments() -> None:
    instruments = load_instruments()
    assert len(instruments) == EXPECTED_UNIVERSE_SIZE
    assert len(set(instruments)) == EXPECTED_UNIVERSE_SIZE


def test_psygridevents_receives_exactly_the_same_universe_as_psygrid() -> None:
    local = set(load_instruments())
    reference = set(_psygrid_reference_symbols())
    assert local == reference


def test_canonical_source_provenance_is_recorded() -> None:
    document = json.loads(INSTRUMENTS_PATH.read_text(encoding="utf-8"))
    source = document["canonical_source"]
    assert source["repo"] == "zahidshaikmohammed-cmyk/Psygrid"
    assert source["path"] == "stocks.json"
    assert source["universe_id"] == "PSYGRID_990"
    assert source["commit"]
    assert source["synced_at"]


def test_set_equality_check_passes_for_the_real_vendored_universe() -> None:
    report = check_universe_integrity(load_instruments(), reference_symbols=_psygrid_reference_symbols())
    assert report.is_valid is True
    assert report.set_equal_to_reference is True
    assert report.missing_vs_reference == ()
    assert report.extra_vs_reference == ()


def test_duplicate_symbol_is_detected() -> None:
    reference = _psygrid_reference_symbols()
    corrupted = list(reference) + [reference[0]]
    report = check_universe_integrity(corrupted, reference_symbols=reference)
    assert report.is_valid is False
    assert reference[0] in report.duplicates


def test_missing_instrument_is_detected() -> None:
    reference = _psygrid_reference_symbols()
    corrupted = reference[1:]  # drop one symbol
    report = check_universe_integrity(corrupted, reference_symbols=reference)
    assert report.is_valid is False
    assert reference[0] in report.missing_vs_reference


def test_extra_instrument_is_never_silently_accepted() -> None:
    reference = _psygrid_reference_symbols()
    corrupted = list(reference) + ["FAKESYMBOL"]
    report = check_universe_integrity(corrupted, reference_symbols=reference)
    assert report.is_valid is False
    assert "FAKESYMBOL" in report.extra_vs_reference


def test_malformed_symbols_are_detected() -> None:
    reference = _psygrid_reference_symbols()
    corrupted = [""] + reference[1:]
    report = check_universe_integrity(corrupted)
    assert report.is_valid is False
    assert report.malformed


def test_stale_450_universe_fails_closed_instead_of_silently_loading() -> None:
    reference = _psygrid_reference_symbols()
    stale_450 = reference[:450]
    with pytest.raises(CanonicalUniverseUnavailableError) as excinfo:
        assert_universe_integrity(stale_450)
    assert "CANONICAL_UNIVERSE_UNAVAILABLE" in str(excinfo.value)


def test_malformed_and_undersized_universe_fails_closed() -> None:
    with pytest.raises(CanonicalUniverseUnavailableError):
        assert_universe_integrity(["reliance", "RELIANCE"])  # lowercase symbol + wrong count


def test_a_post_expansion_symbol_resolves_through_entity_resolution() -> None:
    # ACGL was added when the universe expanded from 450 to 990; if it were
    # only present in config/instruments.json but not actually wired through
    # to entity resolution, this would fail.
    assert "ACGL" in load_instruments()
    resolver = InstrumentResolver.from_instrument_file(INSTRUMENTS_PATH)
    matches = resolver.resolve("ACGL reports quarterly results")
    assert [match.instrument for match in matches] == ["ACGL"]


def test_existing_450_era_symbols_still_resolve() -> None:
    resolver = InstrumentResolver.from_instrument_file(INSTRUMENTS_PATH)
    matches = resolver.resolve("Reliance Industries wins a major order")
    assert [match.instrument for match in matches] == ["RELIANCE"]
