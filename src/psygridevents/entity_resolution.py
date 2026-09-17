from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from .normalize import canonical_text


@dataclass(frozen=True)
class EntityMatch:
    instrument: str
    alias: str
    start: int
    end: int
    confidence: float


class InstrumentResolver:
    """Deterministic ticker/alias resolver for the configured universe.

    This is intentionally conservative. It never infers a company from a vague
    noun; only configured aliases are eligible. A future issuer-master adapter
    can add verified company names without changing this contract.
    """

    def __init__(self, aliases: dict[str, list[str]]) -> None:
        self.aliases = {
            symbol: sorted({canonical_text(symbol), *(canonical_text(a) for a in values)}, key=len, reverse=True)
            for symbol, values in aliases.items()
        }

    @classmethod
    def from_instrument_file(cls, path: str | Path, alias_file: str | Path | None = None) -> "InstrumentResolver":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        aliases = {symbol: [symbol] for symbol in data["instruments"]}
        if alias_file and Path(alias_file).exists():
            configured = json.loads(Path(alias_file).read_text(encoding="utf-8"))
            for symbol, values in configured.get("aliases", {}).items():
                aliases.setdefault(symbol, []).extend(values)
        return cls(aliases)

    def resolve(self, text: str) -> list[EntityMatch]:
        canonical = canonical_text(text)
        matches: list[EntityMatch] = []
        for symbol, aliases in self.aliases.items():
            for alias in aliases:
                if not alias:
                    continue
                pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
                for match in re.finditer(pattern, canonical):
                    matches.append(EntityMatch(symbol, alias, match.start(), match.end(), 0.99 if alias == canonical_text(symbol) else 0.90))
        return self._remove_overlaps(matches)

    @staticmethod
    def _remove_overlaps(matches: list[EntityMatch]) -> list[EntityMatch]:
        chosen: list[EntityMatch] = []
        for candidate in sorted(matches, key=lambda m: (-m.confidence, -(m.end - m.start), m.start)):
            if any(candidate.start < other.end and other.start < candidate.end for other in chosen):
                continue
            chosen.append(candidate)
        return sorted(chosen, key=lambda m: m.start)
