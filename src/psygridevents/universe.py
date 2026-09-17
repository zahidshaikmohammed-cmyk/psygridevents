from __future__ import annotations

import json
from pathlib import Path


DEFAULT_UNIVERSE_PATH = Path(__file__).resolve().parents[2] / "config" / "instruments.json"


def load_instruments(path: Path = DEFAULT_UNIVERSE_PATH) -> tuple[str, ...]:
    """Load and validate the canonical monitored instrument universe."""
    data = json.loads(path.read_text(encoding="utf-8"))
    instruments = data.get("instruments")
    if not isinstance(instruments, list) or not instruments:
        raise ValueError("config/instruments.json must contain a non-empty instruments list")

    normalized = tuple(str(symbol).strip().upper() for symbol in instruments)
    if any(not symbol for symbol in normalized):
        raise ValueError("Instrument symbols cannot be empty")
    if len(normalized) != len(set(normalized)):
        raise ValueError("Instrument universe contains duplicate symbols")
    return normalized
