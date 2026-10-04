"""Pre-market refresh of official issuer names/ISINs/industries into the `issuers` table.

Sources (official NSE / NSE Indices CSVs, see config/issuer_master_sources.yaml):
  * EQUITY_L.csv            -- SYMBOL, NAME OF COMPANY, ISIN NUMBER for every NSE equity
  * ind_nifty500list.csv     -- Company Name, Industry, Symbol, ISIN Code
Only symbols in the canonical universe are stored. A fetch failure leaves the
previous rows untouched (the curated aliases keep working); nothing is guessed.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .issuer_master import IssuerMasterBuilder
from .providers.base import SourceError
from .providers.http import SourceHttpClient
from .settings import CONFIG_DIR
from .storage import Store

log = logging.getLogger("psygridevents.issuers")


def _row_value(row: dict[str, str], *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if value:
            return value.strip()
    return None


def parse_issuer_csv(text: str, *, universe: frozenset[str]) -> list[dict[str, Any]]:
    rows = IssuerMasterBuilder.parse_csv(text)
    result = []
    for row in rows:
        symbol = (_row_value(row, "symbol") or "").upper()
        if symbol not in universe:
            continue
        result.append({
            "symbol": symbol,
            "company_name": _row_value(row, "name of company", "company name", "company_name"),
            "isin": _row_value(row, "isin number", "isin code", "isin"),
            "industry": _row_value(row, "industry"),
        })
    return result


async def refresh_issuers(store: Store, http: SourceHttpClient, universe: frozenset[str],
                          config_file: Path | None = None) -> dict[str, Any]:
    payload = yaml.safe_load((config_file or CONFIG_DIR / "issuer_master_sources.yaml").read_text(encoding="utf-8")) or {}
    report: dict[str, Any] = {}
    for source_id, source in (payload.get("sources") or {}).items():
        url = source.get("csv_url")
        if not url:
            continue
        try:
            response = await http.get(url, conditional=False)
            rows = parse_issuer_csv(response.content.decode("utf-8-sig", "replace"), universe=universe)
        except (SourceError, Exception) as exc:  # noqa: BLE001
            report[source_id] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
            log.warning("issuer source %s failed: %s", source_id, exc)
            continue
        if not rows:
            report[source_id] = {"ok": False, "error": "no universe symbols found in CSV (format changed?)"}
            continue
        store.replace_issuers(rows, source=source_id)
        report[source_id] = {"ok": True, "rows": len(rows)}
    return report
