from pathlib import Path

from psygridevents.entity_resolution import InstrumentResolver
from psygridevents.issuer_master import IssuerMasterBuilder


ROOT = Path(__file__).resolve().parents[1]


def test_verified_issuer_name_and_sector_are_preserved() -> None:
    builder = IssuerMasterBuilder(ROOT / "config" / "instruments.json")
    records = builder.merge([
        {
            "symbol": "RELIANCE",
            "company_name": "Reliance Industries Limited",
            "isin": "INE002A01018",
            "sector": "Oil Gas & Consumable Fuels",
            "industry": "Petroleum Products",
            "basic_industry": "Refineries & Marketing",
            "verified": "true",
        }
    ])
    reliance = next(record for record in records if record.symbol == "RELIANCE")
    assert reliance.verified is True
    assert reliance.company_name == "Reliance Industries Limited"
    assert reliance.sector == "Oil Gas & Consumable Fuels"
    assert reliance.industry == "Petroleum Products"
    assert reliance.basic_industry == "Refineries & Marketing"


def test_only_verified_issuer_names_enter_entity_resolution() -> None:
    builder = IssuerMasterBuilder(ROOT / "config" / "instruments.json")
    records = builder.merge([
        {
            "symbol": "RELIANCE",
            "company_name": "Reliance Industries Limited",
            "verified": "true",
        },
        {
            "symbol": "HDFCBANK",
            "company_name": "HDFC Bank Limited",
            "verified": "false",
        },
    ])
    base = InstrumentResolver.from_instrument_file(ROOT / "config" / "instruments.json")
    resolver = InstrumentResolver.from_issuer_records(records, base)

    reliance = resolver.resolve("Reliance Industries Limited announced a filing")
    hdfc = resolver.resolve("HDFC Bank Limited announced a filing")

    assert [match.instrument for match in reliance] == ["RELIANCE"]
    assert hdfc == []


def test_missing_master_rows_remain_unverified_and_do_not_get_guessed() -> None:
    builder = IssuerMasterBuilder(ROOT / "config" / "instruments.json")
    records = builder.merge([])
    reliance = next(record for record in records if record.symbol == "RELIANCE")
    assert reliance.verified is False
    assert reliance.company_name is None
    assert reliance.sector is None
