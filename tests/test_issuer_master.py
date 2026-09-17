from pathlib import Path

from psygridevents.issuer_master import IssuerMasterBuilder


ROOT = Path(__file__).resolve().parents[1]


def test_issuer_master_preserves_canonical_universe_and_does_not_guess() -> None:
    builder = IssuerMasterBuilder(ROOT / "config" / "instruments.json")
    records = builder.merge([
        {"symbol": "RELIANCE", "company_name": "Reliance Industries Limited", "isin": "INE002A01018", "source": "nse"}
    ])

    reliance = next(item for item in records if item.symbol == "RELIANCE")
    hdfc = next(item for item in records if item.symbol == "HDFCBANK")

    assert reliance.company_name == "Reliance Industries Limited"
    assert reliance.verified is True
    assert hdfc.company_name is None
    assert hdfc.verified is False
    assert len(records) == len(builder.universe)
