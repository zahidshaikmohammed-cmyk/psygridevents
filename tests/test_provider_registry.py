from pathlib import Path

from psygridevents.provider_registry import build_rss_adapters, load_provider_specs


ROOT = Path(__file__).resolve().parents[1]


def test_provider_catalog_contains_required_first_party_sources() -> None:
    specs = load_provider_specs(ROOT / "config" / "providers.json")
    ids = {spec.id for spec in specs}
    assert {"nse_rss", "sebi_rss", "rbi_rss", "pib_rss", "bse_corporate_data"} <= ids


def test_first_party_sources_have_priority_tier_zero() -> None:
    specs = load_provider_specs(ROOT / "config" / "providers.json")
    by_id = {spec.id: spec for spec in specs}
    for provider_id in ("nse_rss", "sebi_rss", "rbi_rss", "pib_rss", "bse_corporate_data"):
        assert by_id[provider_id].tier == 0


def test_registry_does_not_activate_catalogue_without_verified_feed_url() -> None:
    specs = load_provider_specs(ROOT / "config" / "providers.json")
    adapters = build_rss_adapters(specs)
    ids = {adapter.spec.provider_id for adapter in adapters}
    assert "nse_rss" not in ids
    assert "rbi_rss" not in ids
    assert "sebi_rss" in ids
    assert "pib_rss" in ids
