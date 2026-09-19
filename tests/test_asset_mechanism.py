from pathlib import Path

from psygridevents.asset_mechanism import AssetMechanismEngine
from psygridevents.semantic import SemanticEvent

ROOT = Path(__file__).resolve().parents[1]
EXPOSURE_RULES = ROOT / "config" / "exposure_rules.yaml"
DIRECTION_RULES = ROOT / "config" / "direction_rules.yaml"
MACRO_RULES = ROOT / "config" / "macro_exposure_rules.yaml"


def event(**overrides) -> SemanticEvent:
    values = dict(
        event_id="event-1", story_id="story-1", event_type="order", trigger="order awarded",
        event_time=None, instruments=("RELIANCE",), participants=("reliance",),
        magnitude=None, direct_effect="revenue increase expected", indirect_effect=None,
        competitor_effect=None, supply_chain_effect=None, time_horizon=None,
        novelty_status="not_assessed", surprise_status="not_assessed", modality="asserted",
        negated=False, extraction_confidence=0.9, evidence=(), uncertainty=(), market_mechanism=None,
    )
    values.update(overrides)
    return SemanticEvent(**values)


def engine(issuer_metadata_file=None) -> AssetMechanismEngine:
    return AssetMechanismEngine(EXPOSURE_RULES, DIRECTION_RULES, issuer_metadata_file, MACRO_RULES)


def test_direct_company_event_resolves_configured_symbol_and_mechanism() -> None:
    mappings = engine().assess(event())
    assert len(mappings) == 1
    mapping = mappings[0]
    assert mapping.asset == "RELIANCE"
    assert mapping.asset_type == "instrument"
    assert mapping.exposure_type == "direct"
    assert mapping.mechanism == "explicitly_named_in_event"
    assert mapping.expected_direction == "positive"
    assert mapping.resolved is True


def test_unresolved_issuer_never_guesses_a_symbol() -> None:
    mappings = engine().assess(event(instruments=(), event_type="order"))
    assert len(mappings) == 1
    mapping = mappings[0]
    assert mapping.resolved is False
    assert mapping.asset is None
    assert mapping.asset_type == "unresolved"


def test_verified_company_alias_and_sector_metadata_produce_second_order_link(tmp_path) -> None:
    metadata = tmp_path / "issuers.yaml"
    metadata.write_text("issuers:\n  RELIANCE:\n    sectors: [energy]\n", encoding="utf-8")
    mappings = engine(metadata).assess(event(event_type="commodity", trigger="oil price rise", direct_effect=None))
    assets = {m.asset for m in mappings}
    assert "RELIANCE" in assets
    assert "energy" in assets
    sector_mapping = next(m for m in mappings if m.asset == "energy")
    assert sector_mapping.asset_type == "sector"
    assert sector_mapping.mechanism == "commodity_price_to_input_cost"


def test_macro_event_without_instrument_uses_only_configured_sector_index() -> None:
    mappings = engine().assess(
        event(instruments=(), event_type="central_bank", trigger="repo rate cut", direct_effect="financing conditions improve")
    )
    assert len(mappings) == 1
    mapping = mappings[0]
    assert mapping.resolved is True
    assert mapping.asset_type == "index"
    assert mapping.asset == "NIFTY FINANCIAL SERVICES"
    assert mapping.basis == "configured_macro_channel"
    assert any("no single configured instrument" in note for note in mapping.uncertainty)


def test_macro_event_never_arbitrarily_assigns_a_single_stock() -> None:
    mappings = engine().assess(event(instruments=(), event_type="central_bank"))
    for mapping in mappings:
        assert mapping.asset_type != "instrument"


def test_unconfigured_event_type_without_instrument_stays_unresolved() -> None:
    mappings = engine().assess(event(instruments=(), event_type="legal"))
    assert len(mappings) == 1
    assert mappings[0].resolved is False


def test_negated_event_is_blocked_end_to_end() -> None:
    mappings = engine().assess(event(negated=True, modality="negated"))
    assert mappings[0].resolved is False
    assert mappings[0].basis == "blocked"


def test_primary_prefers_direct_instrument_over_sector(tmp_path) -> None:
    metadata = tmp_path / "issuers.yaml"
    metadata.write_text("issuers:\n  RELIANCE:\n    sectors: [energy]\n", encoding="utf-8")
    mappings = engine(metadata).assess(event(event_type="commodity", trigger="oil price rise", direct_effect=None))
    primary = AssetMechanismEngine.primary(mappings)
    assert primary is not None
    assert primary.asset == "RELIANCE"
    assert primary.exposure_type == "direct"
