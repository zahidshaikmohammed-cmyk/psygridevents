from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .issuer_master import IssuerRecord
from .normalize import canonical_text


@dataclass(frozen=True)
class EntityMatch:
    instrument: str
    alias: str
    start: int
    end: int
    confidence: float


class InstrumentResolver:
    """Deterministic resolver for configured tickers and verified issuer names.

    Only aliases explicitly supplied by the canonical universe or verified issuer
    master are eligible. No company/sector inference is performed from vague text.
    """

    def __init__(self, aliases: dict[str, list[str]]) -> None:
        self.aliases = {
            symbol: sorted(
                {canonical_text(symbol), *(canonical_text(a) for a in values)},
                key=len,
                reverse=True,
            )
            for symbol, values in aliases.items()
        }

    @classmethod
    def from_instrument_file(
        cls, path: str | Path, alias_file: str | Path | None = None
    ) -> "InstrumentResolver":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        aliases = {symbol: [symbol] for symbol in data["instruments"]}
        if alias_file and Path(alias_file).exists():
            configured = json.loads(Path(alias_file).read_text(encoding="utf-8"))
            for symbol, values in configured.get("aliases", {}).items():
                aliases.setdefault(symbol, []).extend(values)
        return cls(aliases)

    @classmethod
    def from_issuer_records(
        cls, records: Iterable[IssuerRecord], base: "InstrumentResolver"
    ) -> "InstrumentResolver":
        """Return a resolver extended only with verified issuer names."""
        aliases = {symbol: list(values) for symbol, values in base.aliases.items()}
        for record in records:
            if not record.verified or not record.company_name:
                continue
            if record.symbol not in aliases:
                continue
            aliases[record.symbol].append(record.company_name)
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
                    confidence = 0.99 if alias == canonical_text(symbol) else 0.95
                    matches.append(EntityMatch(symbol, alias, match.start(), match.end(), confidence))
        return self._remove_overlaps(matches)

    @staticmethod
    def _remove_overlaps(matches: list[EntityMatch]) -> list[EntityMatch]:
        chosen: list[EntityMatch] = []
        for candidate in sorted(matches, key=lambda m: (-m.confidence, -(m.end - m.start), m.start)):
            if any(candidate.start < other.end and other.start < candidate.end for other in chosen):
                continue
            chosen.append(candidate)
        return sorted(chosen, key=lambda m: m.start)


# ---------------------------------------------------------------------------
# Production resolver (names, aliases, symbols, NSE filing links) with
# false-match guards. The InstrumentResolver above is kept unchanged for the
# legacy CP0-CP11 pipeline and its tests.
# ---------------------------------------------------------------------------

_CORPORATE_SUFFIX = re.compile(r"(?i)[\s,]+(limited|ltd\.?|plc|inc\.?)\s*$")
_WORD = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class ResolvedEntity:
    symbol: str
    matched_text: str
    basis: str  # nse_filing_link | exchange_tag | official_name | curated_name | alias:<kind> | symbol_token | bse_code
    confidence: float
    start: int
    end: int


@dataclass(frozen=True)
class _AliasPattern:
    symbol: str
    text: str
    basis: str
    confidence: float
    regex: re.Pattern
    proper_noun: bool


def _alias_regex(alias: str) -> re.Pattern:
    words = _WORD.findall(alias)
    if not words:
        return re.compile(r"(?!x)x")
    if "&" in alias and len(words) <= 3 and len(alias) <= 12:
        body = re.escape(alias).replace(r"\ ", r"\s+")
    else:
        body = r"[\s.\-&,'’]+".join(re.escape(word) for word in words)
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def strip_corporate_suffix(name: str) -> str:
    return _CORPORATE_SUFFIX.sub("", name.strip()).strip()


class EntityResolver:
    """Resolve companies/securities in text without weak name-only coincidences.

    Guards against false matches:
    * Names/aliases must appear as proper nouns (every word capitalised in
      the source text), so lowercase prose ("crude oil India imports") never
      matches an issuer ("Oil India").
    * Bare symbol tokens resolve only when written in uppercase, are not an
      ambiguous/common-word symbol, and the text is not shouting in all caps.
      Ambiguous symbols need an exchange qualifier ("NSE: OIL").
    * Matches inside configured blocked phrases ("Reserve Bank of India"
      contains "Bank of India") are discarded.
    * Overlapping matches keep the longest, most confident one.
    * Aliases for symbols outside the canonical universe are never loaded.
    """

    MIN_CONFIDENCE = 0.6

    def __init__(
        self,
        universe: Iterable[str],
        *,
        official_names: dict[str, str] | None = None,
        curated: dict[str, Any] | None = None,
        bse_codes: dict[str, str] | None = None,
    ) -> None:
        self.universe = frozenset(str(symbol).upper() for symbol in universe)
        curated = curated or {}
        self.blocked = [
            _alias_regex(phrase) for phrase in curated.get("blocked_phrases", []) or [] if str(phrase).strip()
        ]
        self.ambiguous = frozenset(str(s).upper() for s in curated.get("ambiguous_symbols", []) or [])
        self.bse_codes = {code: symbol for code, symbol in (bse_codes or {}).items() if symbol in self.universe}
        self.dropped: list[str] = []
        patterns: list[_AliasPattern] = []
        seen: set[tuple[str, str]] = set()

        def add(symbol: str, text: str, basis: str, confidence: float) -> None:
            text = text.strip()
            key = (symbol, text.lower())
            if not text or key in seen or confidence < self.MIN_CONFIDENCE:
                return
            seen.add(key)
            patterns.append(_AliasPattern(symbol, text, basis, confidence, _alias_regex(text), True))

        for symbol, name in (official_names or {}).items():
            symbol = symbol.upper()
            if symbol not in self.universe or not name:
                continue
            add(symbol, name, "official_name", 0.97)
            short = strip_corporate_suffix(name)
            if short and short != name and len(short) >= 4:
                add(symbol, short, "official_name", 0.94)

        for symbol, entry in (curated.get("issuers", {}) or {}).items():
            symbol = str(symbol).upper()
            if symbol not in self.universe:
                self.dropped.append(symbol)
                continue
            for name in entry.get("names", []) or []:
                add(symbol, str(name), "curated_name", 0.93)
            for alias in entry.get("aliases", []) or []:
                add(symbol, str(alias["alias"]), f"alias:{alias.get('kind', 'alias')}", float(alias.get("confidence", 0.8)))

        # Index patterns by their first lowercase word to avoid scanning every alias.
        self._by_first_word: dict[str, list[_AliasPattern]] = {}
        for pattern in patterns:
            words = _WORD.findall(pattern.text)
            if words:
                self._by_first_word.setdefault(words[0].lower(), []).append(pattern)
        self._symbol_regex = re.compile(r"(?<![A-Za-z0-9&\-])([A-Z][A-Z0-9&\-]{1,19})(?![A-Za-z0-9&\-])")
        self._exchange_tag = re.compile(r"(?i)\b(?:NSE|BSE)\s*[:\-]\s*([A-Za-z0-9&\-]{2,20})")

    @classmethod
    def from_files(
        cls, universe: Iterable[str], alias_file: str | Path | None, official_names: dict[str, str] | None = None
    ) -> "EntityResolver":
        curated: dict[str, Any] = {}
        if alias_file and Path(alias_file).exists():
            import yaml

            curated = yaml.safe_load(Path(alias_file).read_text(encoding="utf-8")) or {}
        return cls(universe, official_names=official_names, curated=curated)

    @staticmethod
    def _proper_noun(span: str) -> bool:
        words = re.findall(r"[A-Za-z][A-Za-z0-9']*", span)
        return bool(words) and all(word[0].isupper() or word.lower() in {"of", "and", "the"} for word in words)

    def _blocked(self, text: str, start: int, end: int) -> bool:
        for regex in self.blocked:
            for match in regex.finditer(text):
                if match.start() <= start and end <= match.end() and (match.end() - match.start()) > (end - start):
                    return True
        return False

    def resolve(
        self, text: str, *, url: str | None = None, nse_filing_symbol: str | None = None
    ) -> list[ResolvedEntity]:
        text = text or ""
        found: list[ResolvedEntity] = []

        if nse_filing_symbol and nse_filing_symbol.upper() in self.universe:
            found.append(ResolvedEntity(nse_filing_symbol.upper(), nse_filing_symbol, "nse_filing_link", 0.99, -1, -1))

        for match in self._exchange_tag.finditer(text):
            token = match.group(1).upper()
            if token in self.universe:
                found.append(ResolvedEntity(token, match.group(0), "exchange_tag", 0.97, match.start(), match.end()))
            elif token.isdigit() and token in self.bse_codes:
                found.append(
                    ResolvedEntity(self.bse_codes[token], match.group(0), "bse_code", 0.95, match.start(), match.end())
                )

        lowered_words = {word.lower() for word in _WORD.findall(text)}
        for first_word in lowered_words & self._by_first_word.keys():
            for pattern in self._by_first_word[first_word]:
                for match in pattern.regex.finditer(text):
                    span = match.group(0)
                    if pattern.proper_noun and not self._proper_noun(span):
                        continue
                    if self._blocked(text, match.start(), match.end()):
                        continue
                    found.append(
                        ResolvedEntity(pattern.symbol, span, pattern.basis, pattern.confidence, match.start(), match.end())
                    )

        letters = [char for char in text if char.isalpha()]
        shouting = bool(letters) and sum(char.isupper() for char in letters) / len(letters) > 0.6
        if not shouting:
            for match in self._symbol_regex.finditer(text):
                token = match.group(1)
                if token not in self.universe or token in self.ambiguous or len(token) < 4:
                    continue
                if self._blocked(text, match.start(), match.end()):
                    continue
                found.append(ResolvedEntity(token, token, "symbol_token", 0.9, match.start(), match.end()))

        return self._select(found)

    @staticmethod
    def _select(found: list[ResolvedEntity]) -> list[ResolvedEntity]:
        chosen: list[ResolvedEntity] = []
        for candidate in sorted(found, key=lambda m: (-m.confidence, -(m.end - m.start), m.start)):
            if candidate.start >= 0 and any(
                other.start >= 0 and candidate.start < other.end and other.start < candidate.end for other in chosen
            ):
                continue
            chosen.append(candidate)
        best: dict[str, ResolvedEntity] = {}
        for item in chosen:
            if item.symbol not in best or item.confidence > best[item.symbol].confidence:
                best[item.symbol] = item
        return sorted(best.values(), key=lambda m: (-m.confidence, m.start))
