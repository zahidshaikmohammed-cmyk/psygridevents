"""Deterministic event classification for Indian equities (config/event_classification.yaml)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .settings import CONFIG_DIR

DEFAULT_RULES = CONFIG_DIR / "event_classification.yaml"

# Generic disclosure subtypes that a routine-noise match on the title overrides
# (e.g. "Newspaper publication of financial results" is an ad, not the results).
_WEAK_SUBTYPES = {"results_filed", "board_meeting_outcome", "board_meeting_intimation", "press_release", "dividend"}


@dataclass(frozen=True)
class Classification:
    subtype: str
    event_type: str
    direction: str  # positive | negative | mixed | neutral | unknown (for the directly named issuer)
    direction_confidence: float
    materiality: float
    horizon: str
    routine: bool
    factor: str | None
    factor_direction: str | None
    matched_text: str


@dataclass(frozen=True)
class _Rule:
    subtype: str
    event_type: str
    direction: str
    direction_confidence: float
    materiality: float
    horizon: str
    routine: bool
    factor: str | None
    factor_direction: str | None
    regex: re.Pattern


UNCLASSIFIED = Classification(
    subtype="unclassified", event_type="corporate", direction="unknown", direction_confidence=0.0,
    materiality=0.1, horizon="unknown", routine=False, factor=None, factor_direction=None, matched_text="",
)


class EventClassifier:
    def __init__(self, rules_file: str | Path = DEFAULT_RULES) -> None:
        payload = yaml.safe_load(Path(rules_file).read_text(encoding="utf-8")) or {}
        self.rules: list[_Rule] = []
        for item in payload.get("rules", []) or []:
            self.rules.append(
                _Rule(
                    subtype=item["subtype"],
                    event_type=item["event_type"],
                    direction=item.get("direction", "unknown"),
                    direction_confidence=float(item.get("direction_confidence", 0.0)),
                    materiality=float(item.get("materiality", 0.2)),
                    horizon=item.get("horizon", "unknown"),
                    routine=bool(item.get("routine", False)),
                    factor=item.get("factor"),
                    factor_direction=item.get("factor_direction"),
                    regex=re.compile(item["pattern"]),
                )
            )

    def classify_all(self, title: str, body: str = "") -> list[Classification]:
        text = f"{title}. {body}".strip()
        results: list[Classification] = []
        seen: set[str] = set()
        for rule in self.rules:
            match = rule.regex.search(text)
            if not match or rule.subtype in seen:
                continue
            seen.add(rule.subtype)
            results.append(
                Classification(
                    subtype=rule.subtype, event_type=rule.event_type, direction=rule.direction,
                    direction_confidence=rule.direction_confidence, materiality=rule.materiality, horizon=rule.horizon,
                    routine=rule.routine, factor=rule.factor, factor_direction=rule.factor_direction,
                    matched_text=match.group(0)[:200],
                )
            )
        return results

    def classify(self, title: str, body: str = "") -> tuple[Classification, list[Classification]]:
        """Return (primary, all_matches). Never raises; unknown text is UNCLASSIFIED."""
        matches = self.classify_all(title, body)
        if not matches:
            return UNCLASSIFIED, []
        routine = [item for item in matches if item.routine]
        substantive = [item for item in matches if not item.routine]
        if not substantive:
            return routine[0], matches
        primary = substantive[0]
        # Resolve conflicting directional subtypes from the same family
        # (e.g. both results_positive and results_negative): mixed, never forced.
        opposite = {
            ("results_positive", "results_negative"), ("business_update_positive", "business_update_negative"),
            ("credit_rating_upgrade", "credit_rating_downgrade"), ("crude_up", "crude_down"),
            ("metals_up", "metals_down"), ("usfda_favourable", "usfda_adverse"),
        }
        subtypes = {item.subtype for item in substantive}
        for left, right in opposite:
            if left in subtypes and right in subtypes:
                primary = Classification(
                    subtype=f"{left.rsplit('_', 1)[0]}_mixed", event_type=primary.event_type, direction="mixed",
                    direction_confidence=0.3, materiality=primary.materiality, horizon=primary.horizon,
                    routine=False, factor=primary.factor, factor_direction=None,
                    matched_text=primary.matched_text,
                )
                break
        if routine and primary.subtype in _WEAK_SUBTYPES:
            title_routine = [item for item in self.classify_all(title) if item.routine]
            if title_routine:
                return title_routine[0], matches
        return primary, matches
