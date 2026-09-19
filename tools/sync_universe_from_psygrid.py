#!/usr/bin/env python3
"""Sync psygridevents' vendored instrument universe from canonical Psygrid.

Psygrid (zahidshaikmohammed-cmyk/Psygrid) owns the live 990-stock universe in
its `stocks.json` (validated by Psygrid's own config.py and
tests/test_universe_contract.py). psygridevents does not maintain an
independent universe: this script is the ONLY place that updates
config/instruments.json, and it does so by reading Psygrid's actual
canonical file -- never by hand-editing a second list.

Usage:
    python tools/sync_universe_from_psygrid.py --psygrid-path /path/to/Psygrid
    python tools/sync_universe_from_psygrid.py --psygrid-url https://raw.githubusercontent.com/zahidshaikmohammed-cmyk/Psygrid/main/stocks.json

Fails closed (prints CANONICAL_UNIVERSE_UNAVAILABLE and exits non-zero)
rather than writing a partial/invalid universe if the source cannot be read
or does not satisfy the canonical contract.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from psygridevents.universe_integrity import (  # noqa: E402
    CanonicalUniverseUnavailableError,
    assert_universe_integrity,
)

CANONICAL_REPO = "zahidshaikmohammed-cmyk/Psygrid"
CANONICAL_SOURCE_PATH = "stocks.json"
CANONICAL_UNIVERSE_ID = "PSYGRID_990"
INSTRUMENTS_PATH = ROOT / "config" / "instruments.json"


def _load_from_path(psygrid_path: Path) -> dict:
    source_file = psygrid_path / CANONICAL_SOURCE_PATH
    if not source_file.exists():
        raise CanonicalUniverseUnavailableError(f"{source_file} does not exist")
    try:
        return json.loads(source_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanonicalUniverseUnavailableError(f"could not read/parse {source_file}: {exc}") from exc


def _load_from_url(url: str) -> dict:
    import httpx

    try:
        response = httpx.get(url, timeout=20.0, headers={"User-Agent": "psygridevents-universe-sync/1.0"})
        response.raise_for_status()
        return response.json()
    except Exception as exc:  # noqa: BLE001 - surfaced as CANONICAL_UNIVERSE_UNAVAILABLE
        raise CanonicalUniverseUnavailableError(f"could not fetch {url}: {exc}") from exc


def _git_commit(psygrid_path: Path | None) -> str:
    if psygrid_path is None:
        return "unknown"
    try:
        result = subprocess.run(
            ["git", "-C", str(psygrid_path), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return "unknown"


def _validate_canonical_payload(payload: dict) -> list[str]:
    if not isinstance(payload, dict):
        raise CanonicalUniverseUnavailableError("Psygrid stocks.json is not a JSON object")
    if payload.get("universe") != CANONICAL_UNIVERSE_ID:
        raise CanonicalUniverseUnavailableError(
            f"expected universe={CANONICAL_UNIVERSE_ID!r}, got {payload.get('universe')!r}"
        )
    if str(payload.get("exchange", "")).upper() != "NSE":
        raise CanonicalUniverseUnavailableError(f"expected exchange=NSE, got {payload.get('exchange')!r}")
    if str(payload.get("instrument", "")).upper() != "EQUITY":
        raise CanonicalUniverseUnavailableError(f"expected instrument=EQUITY, got {payload.get('instrument')!r}")
    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise CanonicalUniverseUnavailableError("stocks.json contains no non-empty 'symbols' array")
    return [str(symbol).strip().upper() for symbol in symbols]


def sync(*, psygrid_path: Path | None, psygrid_url: str | None) -> None:
    if psygrid_path is not None:
        payload = _load_from_path(psygrid_path)
    elif psygrid_url is not None:
        payload = _load_from_url(psygrid_url)
    else:
        raise CanonicalUniverseUnavailableError("neither --psygrid-path nor --psygrid-url was supplied")

    symbols = _validate_canonical_payload(payload)
    # Enforce the exact same fail-closed contract psygridevents itself will
    # later require via universe.load_instruments() -- reject here, before
    # ever writing the file, rather than after.
    assert_universe_integrity(symbols)

    commit = _git_commit(psygrid_path)
    existing = json.loads(INSTRUMENTS_PATH.read_text(encoding="utf-8")) if INSTRUMENTS_PATH.exists() else {}
    document = {
        "version": "2.0.0",
        "exchange": "NSE",
        "canonical_source": {
            "repo": CANONICAL_REPO,
            "path": CANONICAL_SOURCE_PATH,
            "universe_id": CANONICAL_UNIVERSE_ID,
            "commit": commit,
            "synced_at": datetime.now(timezone.utc).isoformat(),
        },
        "instruments": symbols,
    }
    previous_instruments = existing.get("instruments") if isinstance(existing, dict) else None
    INSTRUMENTS_PATH.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"Synced {len(symbols)} instruments from {CANONICAL_REPO}/{CANONICAL_SOURCE_PATH} (commit={commit})")
    if isinstance(previous_instruments, list):
        previous_set = {str(s).strip().upper() for s in previous_instruments}
        current_set = set(symbols)
        added = sorted(current_set - previous_set)
        removed = sorted(previous_set - current_set)
        print(f"Previous universe size: {len(previous_instruments)}; new size: {len(symbols)}")
        if added:
            print(f"Added ({len(added)}): {', '.join(added[:20])}{' ...' if len(added) > 20 else ''}")
        if removed:
            print(f"Removed ({len(removed)}): {', '.join(removed[:20])}{' ...' if len(removed) > 20 else ''}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--psygrid-path", type=Path, default=None, help="Local checkout of zahidshaikmohammed-cmyk/Psygrid")
    parser.add_argument("--psygrid-url", type=str, default=None, help="Raw URL to Psygrid's stocks.json")
    args = parser.parse_args()

    try:
        sync(psygrid_path=args.psygrid_path, psygrid_url=args.psygrid_url)
    except CanonicalUniverseUnavailableError as exc:
        print(str(exc))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
