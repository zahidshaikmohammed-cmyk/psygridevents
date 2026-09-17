from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx


@dataclass(frozen=True)
class IssuerRecord:
    symbol: str
    company_name: str | None
    isin: str | None
    security_code: str | None
    series: str | None
    sector: str | None
    industry: str | None
    basic_industry: str | None
    source: str
    verified: bool


class IssuerMasterBuilder:
    """Build a verified identifier/sector layer without altering the supplied universe."""

    def __init__(self, universe_file: str | Path) -> None:
        self.universe = self._load_universe(universe_file)

    @staticmethod
    def _load_universe(path: str | Path) -> tuple[str, ...]:
        import json

        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return tuple(payload.get("instruments", []))

    @staticmethod
    def _verified(row: dict[str, str]) -> bool:
        value = row.get("verified")
        if value is None:
            return True
        return str(value).strip().lower() in {"1", "true", "yes", "verified"}

    def merge(self, rows: Iterable[dict[str, str]]) -> list[IssuerRecord]:
        by_symbol = {row.get("symbol", "").strip().upper(): row for row in rows if row.get("symbol")}
        records: list[IssuerRecord] = []
        for symbol in self.universe:
            row = by_symbol.get(symbol.upper(), {})
            records.append(
                IssuerRecord(
                    symbol=symbol,
                    company_name=row.get("company_name") or row.get("company name") or None,
                    isin=row.get("isin") or row.get("ISIN") or None,
                    security_code=row.get("security_code") or row.get("security code") or None,
                    series=row.get("series") or None,
                    sector=row.get("sector") or row.get("macro_sector") or None,
                    industry=row.get("industry") or None,
                    basic_industry=row.get("basic_industry") or row.get("basic industry") or None,
                    source=row.get("source", "nse_official") if row else "nse_official",
                    verified=self._verified(row) if row else False,
                )
            )
        return records

    @staticmethod
    def parse_csv(text: str) -> list[dict[str, str]]:
        reader = csv.DictReader(io.StringIO(text))
        return [{str(k).strip().lower(): (v or "").strip() for k, v in row.items()} for row in reader]

    @staticmethod
    def fetch_csv(url: str, *, timeout: float = 20.0) -> list[dict[str, str]]:
        response = httpx.get(
            url,
            timeout=timeout,
            headers={"User-Agent": "PSYGRIDEVENTS/0.2 (+issuer-master)"},
        )
        response.raise_for_status()
        return IssuerMasterBuilder.parse_csv(response.text)
