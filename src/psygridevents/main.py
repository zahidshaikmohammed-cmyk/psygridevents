from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

import yaml

from .delivery import build_intelligence_payload
from .market_data import MarketDataAdapter, NullMarketDataAdapter, PsygridMarketDataAdapter
from .provider_registry import build_rss_adapters, load_provider_specs
from .psygrid_client import DEFAULT_BASE_URL
from .story_engine import StoryEngine, StoryIntelligence
from .universe import load_instruments


ROOT = Path(__file__).resolve().parents[2]


def _load_feeds() -> list[dict]:
    path = ROOT / "config" / "feed_endpoints.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload.get("feeds", [])


def build_market_data_adapter(args: argparse.Namespace) -> MarketDataAdapter:
    if args.market_data == "none":
        return NullMarketDataAdapter()
    return PsygridMarketDataAdapter(args.market_data_url)


def run_pipeline(
    engine: StoryEngine,
    feeds: list[dict],
    market_data: MarketDataAdapter,
    *,
    since: datetime | None = None,
    as_of: datetime | None = None,
) -> tuple[list, list[StoryIntelligence], list, dict]:
    """Run the full CP0-CP11 chain once and return (observations, stories, ranked, payload).

    This is the single implementation shared by --once and --watch so the
    two modes can never silently diverge in what they compute.
    """
    now = as_of or engine.now()
    observations = engine.acquire(feeds, since=since)
    stories = engine.build_stories(observations)
    stories = engine.build_materiality(stories)
    stories = engine.build_prioritization(stories)
    ranked = engine.rank_prioritization(stories)

    # CP8 resolves EVENT -> ASSET first; only resolved assets are ever fetched
    # from the live market-data adapter (event-driven, not a 990-symbol scan).
    stories = engine.build_asset_mechanism(stories)
    market_observations: list = []
    for story in stories:
        for mappings in story.asset_mechanisms:
            for mapping in mappings:
                if mapping.resolved and mapping.asset:
                    market_observations.extend(market_data.observations(mapping.asset, as_of=now))

    stories = engine.build_market_confirmation(stories, market_observations)
    stories = engine.build_event_timing(stories, as_of=now)
    stories = engine.build_market_response(stories, market_observations, as_of=now)
    stories = engine.build_exhaustion(stories)
    stories = engine.build_signals(stories, as_of=now)

    payload = build_intelligence_payload(stories, ranked, generated_at=now)
    return observations, stories, ranked, payload


def _print_coverage(market_data: MarketDataAdapter, instruments: tuple[str, ...], as_of: datetime) -> None:
    if not isinstance(market_data, PsygridMarketDataAdapter):
        return
    report = market_data.universe_coverage(instruments, as_of=as_of)
    if report.error:
        print(f"Live market-data coverage: CANONICAL_MARKET_DATA_UNAVAILABLE ({report.error})")
        return
    print(
        f"Live market-data coverage: configured={report.configured} "
        f"resolved_security_ids={report.resolved_security_ids} "
        f"live_data_received={report.live_data_received} "
        f"stale={report.stale} missing={report.missing}"
    )


def _print_ranked(ranked: list, signal_by_event_id: dict, limit: int) -> None:
    print(f"Ranked intelligence events: {len(ranked)}")
    for item in ranked[: max(0, limit)]:
        print(
            f"- {item.event_id}: score={item.priority_score:.2f} "
            f"class={item.priority_class} coverage={item.coverage:.0%}"
        )
        print(f"  {item.reason}")
        signal = signal_by_event_id.get(item.event_id)
        if signal is None:
            continue
        print(
            f"  ASSET {signal.asset or 'unresolved'} ({signal.asset_type})  "
            f"EVENT {signal.event_type}  MECHANISM {signal.transmission_mechanism or 'unsupported'}"
        )
        print(
            f"  EXPECTED DIRECTION {signal.expected_direction}  "
            f"EVENT STATE {signal.event_state}  MARKET RESPONSE {signal.market_response}  "
            f"EXHAUSTION {signal.exhaustion_state}"
        )
        print(f"  SIGNAL {signal.signal_state} (confidence={signal.signal_strength_or_confidence:.2f})")
        print(f"  TRIGGER {signal.trigger}")
        print(f"  INVALIDATION {signal.invalidation}")
        if signal.uncertainty:
            print(f"  UNCERTAINTY {'; '.join(signal.uncertainty)}")


def _run_once(args: argparse.Namespace) -> None:
    instruments = load_instruments()
    engine = StoryEngine(ROOT / "config" / "instruments.json")
    market_data = build_market_data_adapter(args)
    feeds = _load_feeds()

    observations, stories, ranked, payload = run_pipeline(engine, feeds, market_data)
    now = datetime.fromisoformat(payload["generated_at"])

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        return

    event_count = sum(len(item.semantic_events) for item in stories)
    signal_by_event_id = {signal.event_id: signal for item in stories for signal in item.signals}
    print(f"Raw observations: {len(observations)}")
    print(f"Unique stories: {len(stories)}")
    print(f"Semantic event candidates: {event_count}")
    _print_coverage(market_data, instruments, now)
    _print_ranked(ranked, signal_by_event_id, args.limit)


def _run_watch(args: argparse.Namespace) -> None:
    """Continuous mode: repeatedly acquire, resolve, observe and publish signal changes.

    This is a thin loop around the exact same run_pipeline() --once uses --
    no new CP stage, no separate signal logic. Psygrid does not expose a
    push/streaming feed to external consumers (only plain HTTP JSON meant to
    be polled -- see docs/LIVE_MARKET_DATA_INTEGRATION.md), so polling at a
    configurable interval is the minimal correct client for its contract,
    not an invented complication.
    """
    engine = StoryEngine(ROOT / "config" / "instruments.json")
    market_data = build_market_data_adapter(args)
    feeds = _load_feeds()

    since: datetime | None = None
    last_signal_state: dict[str, str] = {}
    print(f"PSYGRIDEVENTS watch mode: polling every {args.interval}s (Ctrl+C to stop)")
    try:
        while True:
            _observations, stories, _ranked, payload = run_pipeline(engine, feeds, market_data, since=since)
            now = datetime.fromisoformat(payload["generated_at"])
            for story in stories:
                for signal in story.signals:
                    previous = last_signal_state.get(signal.event_id)
                    if previous != signal.signal_state:
                        print(
                            f"[{now.isoformat()}] SIGNAL CHANGE {signal.event_id}: "
                            f"{previous or 'NEW'} -> {signal.signal_state} "
                            f"(asset={signal.asset or 'unresolved'}, {signal.trigger})"
                        )
                        last_signal_state[signal.event_id] = signal.signal_state
            since = now
            time.sleep(max(1.0, args.interval))
    except KeyboardInterrupt:
        print("Watch mode stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="PSYGRIDEVENTS event intelligence engine")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Acquire currently enabled verified feeds once and build intelligence.",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Continuously acquire/resolve/observe/signal on a polling interval instead of running once.",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=60.0,
        help="Polling interval in seconds for --watch (default: 60).",
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
    parser.add_argument(
        "--market-data",
        choices=("psygrid", "none"),
        default="psygrid",
        help="Live market-data source: 'psygrid' (default, consumes Psygrid's public JSON) or "
        "'none' (fail-closed NullMarketDataAdapter, e.g. for offline testing).",
    )
    parser.add_argument(
        "--market-data-url",
        default=DEFAULT_BASE_URL,
        help=f"Base URL for the Psygrid market-data adapter (default: {DEFAULT_BASE_URL}).",
    )
    args = parser.parse_args()

    instruments = load_instruments()
    specs = load_provider_specs()
    adapters = build_rss_adapters(specs)

    if not args.json:
        print(f"PSYGRIDEVENTS event intelligence engine: {len(instruments)} instruments configured")
        print(f"Provider catalog: {len(specs)} providers")
        print(f"Concrete provider adapters ready: {len(adapters)}")

    if args.watch:
        if args.json:
            raise SystemExit("--json is not supported with --watch")
        _run_watch(args)
        return

    if not args.once:
        if args.json:
            raise SystemExit("--json requires --once")
        print("Ingestion boundary: verified source facts only; semantic interpretation remains provenance-linked.")
        print("Run with --once to execute acquisition and build intelligence, or --watch for continuous mode.")
        return

    _run_once(args)


if __name__ == "__main__":
    main()
