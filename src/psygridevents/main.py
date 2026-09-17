from __future__ import annotations

import argparse
from pathlib import Path

import yaml

from .provider_registry import build_rss_adapters, load_provider_specs
from .story_engine import StoryEngine
from .universe import load_instruments


ROOT = Path(__file__).resolve().parents[2]


def _load_feeds() -> list[dict]:
    path = ROOT / "config" / "feed_endpoints.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload.get("feeds", [])


def main() -> None:
    parser = argparse.ArgumentParser(description="PSYGRIDEVENTS event intelligence engine")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Acquire currently enabled verified feeds once and print story diagnostics.",
    )
    args = parser.parse_args()

    instruments = load_instruments()
    specs = load_provider_specs()
    adapters = build_rss_adapters(specs)

    print(f"PSYGRIDEVENTS event intelligence engine: {len(instruments)} instruments configured")
    print(f"Provider catalog: {len(specs)} providers")
    print(f"Concrete provider adapters ready: {len(adapters)}")

    if not args.once:
        print("Ingestion boundary: source facts only; no interpretation is performed.")
        print("Run with --once to execute the verified first-party acquisition pipeline.")
        return

    feeds = _load_feeds()
    engine = StoryEngine(ROOT / "config" / "instruments.json")
    observations = engine.acquire(feeds)
    stories = engine.build_stories(observations)

    print(f"Raw observations: {len(observations)}")
    print(f"Unique stories: {len(stories)}")
    for item in stories[:20]:
        symbols = sorted({match.instrument for match in item.entities})
        print("-", item.story.representative.title)
        print("  sources:", len(item.story.observations), "state:", item.evidence.corroboration_state)
        print("  instruments:", ", ".join(symbols) if symbols else "unresolved")


if __name__ == "__main__":
    main()
