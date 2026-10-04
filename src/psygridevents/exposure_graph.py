"""Reusable 990-stock exposure / transmission graph (config/exposure_graph.yaml)."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from .settings import CONFIG_DIR

log = logging.getLogger("psygridevents.exposure")

DEFAULT_GRAPH_FILE = CONFIG_DIR / "exposure_graph.yaml"
DEFAULT_SECTOR_FILE = CONFIG_DIR / "psygrid_sector_map.json"

RELATIONSHIPS = (
    "DIRECT", "SECTOR", "COMPETITOR", "SUPPLIER", "CUSTOMER", "COMMODITY", "REGULATORY", "MACRO", "SECOND_ORDER",
)
_MACRO_FACTORS = {"policy_rate", "liquidity", "inr", "inflation", "growth", "us_rates"}
_REGULATORY_FACTORS = {"windfall_tax"}


@dataclass(frozen=True)
class Exposure:
    symbol: str
    relationship: str
    mechanism: str
    expected_direction: str  # positive | negative | mixed | unknown
    confidence: float
    materiality: float
    hop: int
    basis: str
    source_symbol: str | None = None
    group: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExposureRequest:
    """What the graph needs to know about one canonical event."""

    direct_symbols: tuple[tuple[str, float], ...]  # (symbol, entity confidence)
    event_type: str
    subtype: str
    direction: str  # documented direction for the directly named issuer
    materiality: float
    text: str
    factor: str | None = None
    factor_direction: str | None = None


def _flip(direction: str) -> str:
    return {"positive": "negative", "negative": "positive"}.get(direction, direction)


def _signed(sign: int, factor_direction: str | None) -> str:
    if sign == 0 or factor_direction not in {"up", "down"}:
        return "unknown"
    effective = sign if factor_direction == "up" else -sign
    return "positive" if effective > 0 else "negative"


class ExposureGraph990:
    def __init__(
        self,
        universe: Iterable[str],
        graph_file: str | Path = DEFAULT_GRAPH_FILE,
        sector_file: str | Path | None = DEFAULT_SECTOR_FILE,
        *,
        max_exposures_per_event: int = 40,
    ) -> None:
        self.universe = frozenset(universe)
        payload = yaml.safe_load(Path(graph_file).read_text(encoding="utf-8")) or {}
        self.dropped: list[str] = []
        self.groups: dict[str, tuple[str, ...]] = {}
        for name, members in (payload.get("peer_groups") or {}).items():
            valid = []
            for symbol in members or []:
                symbol = str(symbol).upper()
                if symbol in self.universe:
                    valid.append(symbol)
                else:
                    self.dropped.append(f"{name}:{symbol}")
            self.groups[name] = tuple(dict.fromkeys(valid))
        self.groups_of: dict[str, list[str]] = {}
        for name, members in self.groups.items():
            for symbol in members:
                self.groups_of.setdefault(symbol, []).append(name)
        self.competitor_rules: dict[str, dict[str, Any]] = payload.get("competitor_rules") or {}
        self.supply_chain: list[dict[str, Any]] = [
            edge for edge in (payload.get("supply_chain") or [])
            if str(edge.get("supplier", "")).upper() in self.universe and str(edge.get("customer", "")).upper() in self.universe
        ]
        self.factor_sensitivities: dict[str, list[dict[str, Any]]] = payload.get("factor_sensitivities") or {}
        self.product_groups: dict[str, list[str]] = payload.get("product_groups") or {}
        self.policy_product_rules: dict[str, dict[str, Any]] = payload.get("policy_product_rules") or {}
        self.regulator_scope: dict[str, dict[str, Any]] = payload.get("regulator_scope") or {}
        self.max_exposures = max_exposures_per_event
        self.sector_of: dict[str, str] = {}
        if sector_file and Path(sector_file).exists():
            sectors = json.loads(Path(sector_file).read_text(encoding="utf-8")).get("sectors", {})
            self.sector_of = {symbol: sector for symbol, sector in sectors.items() if symbol in self.universe}
        if self.dropped:
            log.info("exposure graph dropped %d symbols not in universe: %s", len(self.dropped), ", ".join(self.dropped[:20]))
        self._regulator_regex = {
            name: re.compile(rf"(?<![A-Za-z]){re.escape(name)}(?![A-Za-z])") for name in self.regulator_scope
        }
        self._product_regex = {
            keyword: re.compile(rf"(?i)(?<![a-z]){re.escape(keyword)}s?(?![a-z])") for keyword in self.product_groups
        }

    # ------------------------------------------------------------------ helpers
    def peers(self, symbol: str) -> list[tuple[str, str]]:
        result: list[tuple[str, str]] = []
        for group in self.groups_of.get(symbol, []):
            for member in self.groups[group]:
                if member != symbol:
                    result.append((member, group))
        return result

    def sector(self, symbol: str) -> str | None:
        return self.sector_of.get(symbol)

    def sector_members(self, sector: str) -> list[str]:
        return [symbol for symbol, value in self.sector_of.items() if value == sector]

    # ------------------------------------------------------------------- expand
    def expand(self, request: ExposureRequest) -> list[Exposure]:
        exposures: dict[tuple[str, str], Exposure] = {}

        def add(exposure: Exposure) -> None:
            if exposure.symbol not in self.universe:
                return
            key = (exposure.symbol, exposure.relationship)
            current = exposures.get(key)
            if current is None or exposure.confidence > current.confidence:
                exposures[key] = exposure

        direct_set = {symbol for symbol, _ in request.direct_symbols}
        for symbol, confidence in request.direct_symbols:
            add(Exposure(symbol, "DIRECT", "explicitly_named_in_event", request.direction, round(confidence, 3),
                         round(request.materiality, 3), 0, "entity_resolution"))

        # COMPETITOR read-through (company-specific events only).
        rule = self.competitor_rules.get(request.subtype)
        if rule and request.direction in {"positive", "negative"}:
            relation = rule.get("relation", "same")
            for symbol, _ in request.direct_symbols:
                for peer, group in self.peers(symbol)[:15]:
                    if peer in direct_set:
                        continue
                    direction = request.direction if relation == "same" else _flip(request.direction)
                    add(Exposure(
                        peer, "COMPETITOR", str(rule.get("mechanism", "peer_read_through")), direction,
                        float(rule.get("confidence", 0.3)),
                        round(request.materiality * float(rule.get("materiality_factor", 0.3)), 3), 1,
                        f"peer_group:{group}", source_symbol=symbol, group=group,
                    ))

        # SUPPLIER / CUSTOMER (documented edges only) + SECOND_ORDER via the counterpart's peers.
        for edge in self.supply_chain:
            supplier, customer = str(edge["supplier"]).upper(), str(edge["customer"]).upper()
            confidence = float(edge.get("confidence", 0.5))
            mechanism = str(edge.get("mechanism", "documented_supply_relationship"))
            if customer in direct_set and request.direction in {"positive", "negative"}:
                add(Exposure(supplier, "SUPPLIER", mechanism, request.direction, confidence,
                             round(request.materiality * confidence * 0.6, 3), 1, "documented_supply_chain", customer))
                for peer, group in self.peers(supplier)[:8]:
                    add(Exposure(peer, "SECOND_ORDER", f"{mechanism}_peer", request.direction, round(confidence * 0.4, 3),
                                 round(request.materiality * confidence * 0.2, 3), 2, f"peer_group:{group}", supplier, group))
            if supplier in direct_set and request.direction in {"positive", "negative"}:
                add(Exposure(customer, "CUSTOMER", mechanism, "unknown", round(confidence * 0.6, 3),
                             round(request.materiality * confidence * 0.4, 3), 1, "documented_supply_chain", supplier))

        # COMMODITY / MACRO / REGULATORY factor moves.
        if request.factor and request.factor in self.factor_sensitivities:
            relationship = (
                "MACRO" if request.factor in _MACRO_FACTORS
                else "REGULATORY" if request.factor in _REGULATORY_FACTORS
                else "COMMODITY"
            )
            for channel in self.factor_sensitivities[request.factor]:
                direction = _signed(int(channel.get("sign", 0)), request.factor_direction)
                if request.factor in _REGULATORY_FACTORS and request.direction in {"positive", "negative"} and direction == "unknown":
                    direction = request.direction
                for group in channel.get("groups", []):
                    for member in self.groups.get(group, ()):
                        add(Exposure(
                            member, relationship, str(channel.get("mechanism", f"{request.factor}_channel")), direction,
                            float(channel.get("confidence", 0.4)),
                            round(request.materiality * float(channel.get("confidence", 0.4)), 3), 1,
                            f"factor:{request.factor}:{request.factor_direction or 'unknown'}", group=group,
                        ))

        # Policy events about named products (anti-dumping on steel, sugar export curbs, PLI for solar ...).
        product_rule = self.policy_product_rules.get(request.subtype)
        if product_rule:
            for keyword, regex in self._product_regex.items():
                if not regex.search(request.text):
                    continue
                sign = int(product_rule.get("sign", 0))
                direction = "positive" if sign > 0 else "negative" if sign < 0 else "unknown"
                for group in self.product_groups.get(keyword, []):
                    for member in self.groups.get(group, ()):
                        add(Exposure(
                            member, "REGULATORY", str(product_rule.get("mechanism", "policy_to_sector")), direction,
                            float(product_rule.get("confidence", 0.4)),
                            round(request.materiality * float(product_rule.get("confidence", 0.4)), 3), 1,
                            f"policy_product:{keyword}", group=group,
                        ))

        # Regulator-scoped sector exposure (only when the event names no issuer).
        if not request.direct_symbols and request.event_type in {"regulation", "government_policy", "central_bank", "legal"}:
            for name, regex in self._regulator_regex.items():
                if not regex.search(request.text):
                    continue
                scope = self.regulator_scope[name]
                direction = request.direction if request.direction in {"positive", "negative"} else "unknown"
                for group in scope.get("groups", []):
                    for member in self.groups.get(group, ()):
                        add(Exposure(
                            member, "SECTOR", f"{name.lower().replace(' ', '_')}_regulatory_scope", direction,
                            float(scope.get("confidence", 0.35)),
                            round(request.materiality * float(scope.get("confidence", 0.35)), 3), 1,
                            f"regulator:{name}", group=group,
                        ))

        ranked = sorted(exposures.values(), key=lambda item: (item.hop, -item.confidence, -item.materiality, item.symbol))
        return ranked[: self.max_exposures]
