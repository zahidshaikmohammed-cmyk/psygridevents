from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml

from .delivery import build_intelligence_payload
from .provider_registry import build_rss_adapters, load_provider_specs
from .story_engine import StoryEngine
from .universe import load_instruments


ROOT = Path(__file__).resolve().parents[2]


def _load_feeds() -> list[dict]:
    path = ROOT / "config" / "feed_endpoints.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload.get("feeds", [])


def _print_ranked(ranked: list, limit: int) -> None:
    print(f"Ranked intelligence events: {len(ranked)}")
    for item in ranked[: max(0, limit)]:
        print(
            f"- {item.event_id}: score={item.priority_score:.2f} "
            f"class={item.priority_class} coverage={item.coverage:.0%}"
        )
        print(f"  {item.reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description="PSYGRIDEVENTS event intelligence engine")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Acquire currently enabled verified feeds once and build intelligence.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit the CP7 machine-readable intelligence contract as JSON.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum ranked events shown in human-readable output (default: 20).",
    )
    args = parser.parse_args()

    instruments = load_instruments()
    specs = load_provider_specs()
    adapters = build_rss_adapters(specs)

    if not args.json:
        print(f"PSYGRIDEVENTS event intelligence engine: {len(instruments)} instruments configured")
        print(f"Provider catalog: {len(specs)} providers")
        print(f"Concrete provider adapters ready: {len(adapters)}")

    if not args.once:
        if args.json:
            raise SystemExit("--json requires --once")
        print("Ingestion boundary: verified source facts only; semantic interpretation remains provenance-linked.")
        print("Run with --once to execute acquisition and build intelligence.")
        return

    feeds = _load_feeds()
    engine = StoryEngine(ROOT / "config" / "instruments.json")
    observations = engine.acquire(feeds)
    stories = engine.build_stories(observations)
    stories = engine.build_materiality(stories)
    stories = engine.build_prioritization(stories)
    ranked = engine.rank_prioritization(stories)
    payload = build_intelligence_payload(
        stories,
        ranked,
        generated_at=engine.now(),
    )

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return

    event_count = sum(len(item.semantic_events) for item in stories)
    print(f"Raw observations: {len(observations)}")
    print(f"Unique stories: {len(stories)}")
    print(f"Semantic event candidates: {event_count}")
    _print_ranked(ranked, args.limit)


if __name__ == "__main__":
    main()
