from __future__ import annotations

from .provider_registry import build_rss_adapters, load_provider_specs
from .universe import load_instruments


def main() -> None:
    instruments = load_instruments()
    specs = load_provider_specs()
    adapters = build_rss_adapters(specs)

    print(f"PSYGRIDEVENTS event intelligence engine: {len(instruments)} instruments configured")
    print(f"Provider catalog: {len(specs)} providers")
    print(f"Concrete RSS adapters ready: {len(adapters)}")
    print("Interpretation is intentionally disabled at the ingestion boundary.")
    print("Next layers: normalization -> deduplication -> event clustering -> entity mapping -> intelligence.")


if __name__ == "__main__":
    main()
