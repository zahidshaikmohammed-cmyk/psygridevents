from __future__ import annotations

import json
from pathlib import Path

from .universe_integrity import EXPECTED_UNIVERSE_SIZE, assert_universe_integrity


DEFAULT_UNIVERSE_PATH = Path(__file__).resolve().parents[2] / "config" / "instruments.json"


def load_instruments(path: Path = DEFAULT_UNIVERSE_PATH) -> tuple[str, ...]:
    """Load and validate the canonical monitored instrument universe.

    `config/instruments.json` is a vendored, provenance-stamped mirror of the
    990-symbol universe owned by Psygrid (zahidshaikmohammed-cmyk/Psygrid,
    `stocks.json`) -- see `tools/sync_universe_from_psygrid.py` and
    `docs/UNIVERSE_INTEGRATION.md`. This loader enforces the same strict
    contract Psygrid enforces on itself (exactly `EXPECTED_UNIVERSE_SIZE`
    unique, well-formed symbols) so a corrupted, reverted, or stale local
    file is rejected -- never silently used, and never silently downgraded
    to the historical 450-symbol universe.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    instruments = data.get("instruments")
    if not isinstance(instruments, list) or not instruments:
        raise ValueError("config/instruments.json must contain a non-empty instruments list")

    normalized = tuple(str(symbol).strip().upper() for symbol in instruments)
    # assert_universe_integrity already fails closed on empty/duplicate/malformed
    # symbols and on any count other than EXPECTED_UNIVERSE_SIZE; it raises
    # CanonicalUniverseUnavailableError (message: "CANONICAL_UNIVERSE_UNAVAILABLE: ...").
    assert_universe_integrity(normalized, expected_count=EXPECTED_UNIVERSE_SIZE)
    return normalized
