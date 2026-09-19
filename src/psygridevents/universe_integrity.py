from __future__ import annotations

from dataclasses import dataclass, field

# Psygrid (zahidshaikmohammed-cmyk/Psygrid) is the canonical owner of the
# live stock universe. Its own config.py hardcodes this exact value
# (UNIVERSE_SIZE = 990) and its test suite
# (tests/test_universe_contract.py::test_canonical_stock_universe_is_exactly_990_and_unique)
# enforces it against stocks.json. psygridevents mirrors that same constant
# here so a corrupted, reverted, or stale local universe file is rejected
# deterministically -- never silently accepted as "close enough".
EXPECTED_UNIVERSE_SIZE = 990

CANONICAL_UNIVERSE_UNAVAILABLE = "CANONICAL_UNIVERSE_UNAVAILABLE"


class CanonicalUniverseUnavailableError(RuntimeError):
    """Raised whenever the canonical Psygrid universe cannot be trusted.

    This covers both "could not be loaded at all" (missing file, unreachable
    source, malformed JSON) and "loaded but fails the canonical contract"
    (wrong count, duplicates, malformed symbols). Either way psygridevents
    must refuse to silently run against stale/partial data -- see mission
    requirement: never fall back to the historical 450-symbol universe.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"{CANONICAL_UNIVERSE_UNAVAILABLE}: {reason}")
        self.reason = reason


@dataclass(frozen=True)
class UniverseIntegrityReport:
    """Deterministic, evidence-based verdict on a candidate instrument universe.

    Every failure mode the mission calls out is represented as its own
    explicit field rather than folded into a single boolean, so a caller (or
    a test) can see exactly what is wrong instead of just that something is.
    """

    count: int
    unique_count: int
    duplicates: tuple[str, ...]
    malformed: tuple[str, ...]
    expected_count: int
    count_matches_expected: bool
    is_valid: bool
    missing_vs_reference: tuple[str, ...] = field(default_factory=tuple)
    extra_vs_reference: tuple[str, ...] = field(default_factory=tuple)
    reference_available: bool = False
    set_equal_to_reference: bool | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _find_malformed(symbols: tuple[str, ...]) -> tuple[str, ...]:
    malformed = []
    for symbol in symbols:
        if not isinstance(symbol, str) or not symbol.strip() or symbol != symbol.strip() or symbol != symbol.upper():
            malformed.append(repr(symbol))
    return tuple(malformed)


def _find_duplicates(symbols: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for symbol in symbols:
        if symbol in seen and symbol not in duplicates:
            duplicates.append(symbol)
        seen.add(symbol)
    return tuple(duplicates)


def check_universe_integrity(
    symbols: tuple[str, ...] | list[str],
    *,
    reference_symbols: tuple[str, ...] | list[str] | None = None,
    expected_count: int = EXPECTED_UNIVERSE_SIZE,
) -> UniverseIntegrityReport:
    """Evaluate a candidate universe against the canonical contract.

    This never raises: it always returns a report so callers/tests can
    inspect exactly what failed. Use `assert_universe_integrity` to fail
    closed with `CanonicalUniverseUnavailableError`.
    """
    candidate = tuple(symbols)
    duplicates = _find_duplicates(candidate)
    malformed = _find_malformed(candidate)
    unique_count = len(set(candidate))
    count_matches_expected = len(candidate) == expected_count

    reasons: list[str] = []
    if not count_matches_expected:
        reasons.append(f"expected exactly {expected_count} instruments, found {len(candidate)}")
    if duplicates:
        reasons.append(f"duplicate symbols present: {', '.join(sorted(duplicates))}")
    if malformed:
        reasons.append(f"malformed symbols present: {', '.join(malformed)}")

    missing_vs_reference: tuple[str, ...] = ()
    extra_vs_reference: tuple[str, ...] = ()
    reference_available = reference_symbols is not None
    set_equal_to_reference: bool | None = None
    if reference_available:
        reference_set = set(reference_symbols)
        candidate_set = set(candidate)
        missing_vs_reference = tuple(sorted(reference_set - candidate_set))
        extra_vs_reference = tuple(sorted(candidate_set - reference_set))
        set_equal_to_reference = not missing_vs_reference and not extra_vs_reference
        if missing_vs_reference:
            reasons.append(f"missing vs. canonical reference: {', '.join(missing_vs_reference)}")
        if extra_vs_reference:
            reasons.append(f"extra vs. canonical reference (never guessed/fabricated): {', '.join(extra_vs_reference)}")

    is_valid = (
        count_matches_expected
        and not duplicates
        and not malformed
        and (set_equal_to_reference is not False)
    )

    return UniverseIntegrityReport(
        count=len(candidate),
        unique_count=unique_count,
        duplicates=duplicates,
        malformed=malformed,
        expected_count=expected_count,
        count_matches_expected=count_matches_expected,
        missing_vs_reference=missing_vs_reference,
        extra_vs_reference=extra_vs_reference,
        reference_available=reference_available,
        set_equal_to_reference=set_equal_to_reference,
        is_valid=is_valid,
        reasons=tuple(reasons),
    )


def assert_universe_integrity(
    symbols: tuple[str, ...] | list[str],
    *,
    reference_symbols: tuple[str, ...] | list[str] | None = None,
    expected_count: int = EXPECTED_UNIVERSE_SIZE,
) -> UniverseIntegrityReport:
    """Fail closed: raise CanonicalUniverseUnavailableError on any integrity failure."""
    report = check_universe_integrity(symbols, reference_symbols=reference_symbols, expected_count=expected_count)
    if not report.is_valid:
        raise CanonicalUniverseUnavailableError("; ".join(report.reasons) or "universe failed integrity check")
    return report
