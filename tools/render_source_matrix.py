#!/usr/bin/env python3
"""Render docs/SOURCES.md from config/sources.yaml (the single source of truth)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from psygridevents.providers import load_source_specs  # noqa: E402

HEADER = """# Source matrix

Generated from `config/sources.yaml` by `tools/render_source_matrix.py`; edit the YAML, not this file.

Every `auto` source is **probed on the production host** at start-up and before every session
(`python -m psygridevents.main --probe-sources` shows the live result). A source whose URL is
wrong, retired, blocked, robots-disallowed or not returning the documented structure fails
closed as `DISCONNECTED`/`UNAVAILABLE` and never produces data. No CAPTCHA, login, paywall or
anti-bot challenge is ever attempted.

| Quality | Meaning |
|---|---|
| PRIMARY | The issuer/exchange disclosure itself |
| OFFICIAL | Regulator / central bank / government publishing its own action |
| REPUTABLE_SECONDARY | Established financial media |
| DISCOVERY_ONLY | Aggregators/search: leads only, never a confirmed trading event by themselves |

| id | Source | Kind | Quality | Category | Poll (s) | Verification | Endpoint / note |
|---|---|---|---|---|---|---|---|
"""


def main() -> None:
    specs = load_source_specs(ROOT / "config" / "sources.yaml")
    rows = []
    for spec in specs:
        endpoint = spec.url or (f"from catalogue {spec.catalogue_url}" if spec.catalogue_url else "")
        if spec.kind in ("google_news", "gdelt"):
            endpoint = f"{spec.url} ({len(spec.queries)} queries)"
        if spec.kind == "unavailable":
            endpoint = spec.notes
        poll = "-" if spec.kind == "unavailable" else f"{spec.poll_seconds:g}"
        rows.append(
            f"| `{spec.id}` | {spec.name} | {spec.kind} | {spec.quality.value} | {spec.category} | {poll} | "
            f"{spec.verification} | {endpoint.replace('|', '/')} |"
        )
    (ROOT / "docs" / "SOURCES.md").write_text(HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    print(f"wrote docs/SOURCES.md ({len(rows)} sources)")


if __name__ == "__main__":
    main()
