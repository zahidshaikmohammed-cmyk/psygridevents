from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import yaml

from .catalogue import CatalogueResolutionError, resolve_official_rss_catalogue
from .delivery import build_intelligence_payload
from .provider_registry import build_rss_adapters, load_provider_specs
from .story_engine import StoryEngine
from .universe import load_instruments

ROOT = Path(__file__).resolve().parents[2]
HISTORY_FILE = ROOT / "data" / "state" / "semantic_events.json"


def _load_feeds() -> list[dict]:
    path = ROOT / "config" / "feed_endpoints.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    feeds = list(payload.get("feeds", []))
    resolved: list[dict] = []
    for feed in feeds:
        if feed.get("mode") != "catalogue" or not feed.get("enabled"):
            resolved.append(feed)
            continue
        allowed_hosts = None
        if feed.get("provider_id") == "nse_catalogue":
            allowed_hosts = {"www.nseindia.com", "nsearchives.nseindia.com"}
        try:
            discovered = resolve_official_rss_catalogue(feed["url"], allowed_hosts=allowed_hosts)
        except CatalogueResolutionError as exc:
            print(f"Catalogue resolution skipped: {feed['provider_id']}: {exc}", file=sys.stderr)
            continue
        for index, url in enumerate(discovered, start=1):
            resolved.append({
                "provider_id": f"{feed['provider_id']}_{index}",
                "publisher": feed["publisher"],
                "tier": feed["tier"],
                "category": feed.get("category", "news"),
                "url": url,
                "enabled": True,
            })
    return resolved


def _print_ranked(ranked: list, intelligence: list, limit: int) -> None:
    events = {event.event_id: event for item in intelligence for event in item.semantic_events}
    actionable = [item for item in ranked if events.get(item.event_id) and events[item.event_id].modality == "asserted" and not events[item.event_id].negated]
    print(f"Actionable asserted events: {len(actionable)}")
    print(f"Non-actionable/negated events: {sum(1 for event in events.values() if event.negated or event.modality != 'asserted')}")
    print(f"Ranked intelligence events: {len(ranked)}")
    for item in ranked[: max(0, limit)]:
        event = events.get(item.event_id)
        market_status = event.market_confirmation_status if event else "unknown"
        coverage_note = "insufficient model coverage" if item.coverage < 0.75 else "model coverage available"
        print(f"- {item.event_id}: score={item.priority_score:.2f} class={item.priority_class} coverage={item.coverage:.0%}")
        print(f"  {coverage_note}; market_confirmation={market_status}")
        print(f"  {item.reason}")


def main() -> None:
    parser = argparse.ArgumentParser(description="PSYGRIDEVENTS event intelligence engine")
    parser.add_argument("--once", action="store_true", help="Acquire currently enabled verified feeds once and build intelligence.")
    parser.add_argument("--json", action="store_true", help="Emit the CP7 machine-readable intelligence contract as JSON.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum ranked events shown in human-readable output (default: 20).")
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
    if not args.json:
        print(f"Enabled verified acquisition feeds: {sum(1 for feed in feeds if feed.get('enabled'))}")
    engine = StoryEngine(ROOT / "config" / "instruments.json")
    historical = engine.load_history(HISTORY_FILE)
    observations = engine.acquire(feeds)
    stories = engine.build_stories(observations, historical, as_of=engine.now())
    stories = engine.build_materiality(stories)
    stories = engine.build_transmission(stories)
    stories = engine.build_market_confirmation(stories, ())
    stories = engine.build_prioritization(stories)
    ranked = engine.rank_prioritization(stories)
    current_events = tuple(event for item in stories for event in item.semantic_events)
    engine.save_history(HISTORY_FILE, (*historical, *current_events))
    payload = build_intelligence_payload(stories, ranked, generated_at=engine.now())

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return

    event_count = sum(len(item.semantic_events) for item in stories)
    print(f"Raw observations: {len(observations)}")
    print(f"Unique stories: {len(stories)}")
    print(f"Semantic event candidates: {event_count}")
    print(f"Historical semantic events available: {len(historical)}")
    print("Market confirmation: untested (no live MarketObservation adapter supplied)")
    _print_ranked(ranked, stories, args.limit)


if __name__ == "__main__":
    main()
