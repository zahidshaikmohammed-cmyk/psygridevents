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
        help="Acquire currently enabled verified feeds once and print semantic diagnostics.",
    )
    args = parser.parse_args()

    instruments = load_instruments()
    specs = load_provider_specs()
    adapters = build_rss_adapters(specs)

    print(f"PSYGRIDEVENTS event intelligence engine: {len(instruments)} instruments configured")
    print(f"Provider catalog: {len(specs)} providers")
    print(f"Concrete provider adapters ready: {len(adapters)}")

    if not args.once:
        print("Ingestion boundary: verified source facts only; semantic interpretation remains provenance-linked.")
        print("Run with --once to execute acquisition, story clustering, and semantic event extraction.")
        return

    feeds = _load_feeds()
    engine = StoryEngine(ROOT / "config" / "instruments.json")
    observations = engine.acquire(feeds)
    stories = engine.build_stories(observations)

    event_count = sum(len(item.semantic_events) for item in stories)
    print(f"Raw observations: {len(observations)}")
    print(f"Unique stories: {len(stories)}")
    print(f"Semantic event candidates: {event_count}")

    for item in stories[:20]:
        symbols = sorted({match.instrument for match in item.entities})
        print("-", item.story.representative.title)
        print("  sources:", len(item.story.observations), "state:", item.evidence.corroboration_state)
        print("  instruments:", ", ".join(symbols) if symbols else "unresolved")
        for event in item.semantic_events:
            magnitude = event.magnitude.text if event.magnitude else "unquantified"
            print(
                "  event:",
                event.event_type,
                "trigger=", event.trigger,
                "magnitude=", magnitude,
                "confidence=", event.extraction_confidence,
                "novelty=", event.novelty_status,
                "surprise=", event.surprise_status,
            )


if __name__ == "__main__":
    main()
