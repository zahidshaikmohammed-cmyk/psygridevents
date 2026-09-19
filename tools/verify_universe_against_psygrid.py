#!/usr/bin/env python3
"""Real integration check: does psygridevents' vendored universe still equal
Psygrid's actual, current canonical universe?

Unlike tools/sync_universe_from_psygrid.py (which updates the vendored
file), this script never writes anything. It is the deterministic
universe-integrity check required by the universe-integration mission: it
detects missing symbols, extra symbols, duplicates, and a stale/unavailable
canonical source, and it fails closed (prints CANONICAL_UNIVERSE_UNAVAILABLE
and exits non-zero) rather than reporting success on a guess.

Usage:
    python tools/verify_universe_against_psygrid.py --psygrid-path /path/to/Psygrid
    python tools/verify_universe_against_psygrid.py --psygrid-url https://raw.githubusercontent.com/zahidshaikmohammed-cmyk/Psygrid/main/stocks.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from psygridevents.universe import load_instruments  # noqa: E402
from psygridevents.universe_integrity import (  # noqa: E402
    CanonicalUniverseUnavailableError,
    check_universe_integrity,
)

CANONICAL_UNIVERSE_ID = "PSYGRID_990"


def _load_reference(*, psygrid_path: Path | None, psygrid_url: str | None) -> list[str]:
    if psygrid_path is not None:
        source_file = psygrid_path / "stocks.json"
        if not source_file.exists():
            raise CanonicalUniverseUnavailableError(f"{source_file} does not exist")
        payload = json.loads(source_file.read_text(encoding="utf-8"))
    elif psygrid_url is not None:
        import httpx

        response = httpx.get(psygrid_url, timeout=20.0, headers={"User-Agent": "psygridevents-universe-verify/1.0"})
        response.raise_for_status()
        payload = response.json()
    else:
        raise CanonicalUniverseUnavailableError("neither --psygrid-path nor --psygrid-url was supplied")

    if payload.get("universe") != CANONICAL_UNIVERSE_ID:
        raise CanonicalUniverseUnavailableError(f"unexpected universe id {payload.get('universe')!r}")
    symbols = payload.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        raise CanonicalUniverseUnavailableError("Psygrid source has no non-empty symbols array")
    return [str(symbol).strip().upper() for symbol in symbols]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--psygrid-path", type=Path, default=None)
    parser.add_argument("--psygrid-url", type=str, default=None)
    args = parser.parse_args()

    print("PSYGRIDEVENTS // UNIVERSE INTEGRATION CHECK")
    try:
        local = load_instruments()
    except CanonicalUniverseUnavailableError as exc:
        print(f"LOCAL UNIVERSE: {exc}")
        return 1
    print(f"LOCAL (vendored) universe: {len(local)} instruments")

    try:
        reference = _load_reference(psygrid_path=args.psygrid_path, psygrid_url=args.psygrid_url)
    except CanonicalUniverseUnavailableError as exc:
        print(f"CANONICAL REFERENCE: {exc}")
        return 1
    print(f"CANONICAL (Psygrid) universe: {len(reference)} instruments")

    report = check_universe_integrity(local, reference_symbols=reference)
    print(f"COUNT MATCH: {report.count_matches_expected} ({report.count}/{report.expected_count})")
    print(f"DUPLICATES: {len(report.duplicates)}")
    print(f"MALFORMED: {len(report.malformed)}")
    print(f"MISSING VS CANONICAL: {len(report.missing_vs_reference)}")
    print(f"EXTRA VS CANONICAL: {len(report.extra_vs_reference)}")
    print(f"SET EQUAL TO CANONICAL: {report.set_equal_to_reference}")

    if not report.is_valid:
        print("\nRESULT: FAIL")
        for reason in report.reasons:
            print(f" - {reason}")
        return 1

    print("\nRESULT: PASS -- psygridevents universe == Psygrid canonical universe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
