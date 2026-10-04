"""Optional AI assist for ambiguous, high-materiality, primary-source events.

Off by default. The deterministic engine never depends on it: if the
`anthropic` package is not installed, no API key is set, the daily budget is
spent, or the call fails/refuses, the event simply keeps its deterministic
classification. Its output is advisory: it may only fill an UNKNOWN direction,
at capped confidence (see LiveEngine.apply_ai_assist), and it never overrides
primary-source facts.

Install with:  pip install -e ".[ai]"   and set ANTHROPIC_API_KEY + PSYGRIDEVENTS_AI_ENABLED=1
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from .settings import AISettings

log = logging.getLogger("psygridevents.ai")

_SCHEMA = {
    "type": "object",
    "properties": {
        "direction": {"type": "string", "enum": ["positive", "negative", "mixed", "unknown"]},
        "confidence": {"type": "number"},
        "event_subtype": {"type": "string"},
        "rationale": {"type": "string"},
    },
    "required": ["direction", "confidence", "event_subtype", "rationale"],
    "additionalProperties": False,
}

_SYSTEM = (
    "You classify Indian listed-company disclosures for an event-driven equity research engine. "
    "Judge only from the text given. Report the likely direction of the impact on the named company's "
    "share price as positive, negative, mixed, or unknown. Use unknown when the text does not state "
    "enough (for example a results filing with no figures). Confidence is 0 to 1. Keep the rationale "
    "to one or two sentences and quote the figures you relied on."
)


class AIAssist:
    def __init__(self, settings: AISettings) -> None:
        self.settings = settings
        self._client = None
        self._calls_today = 0
        self._day: date | None = None
        self.last_error: str | None = None
        if settings.available:
            try:
                import anthropic  # optional dependency

                self._client = anthropic.AsyncAnthropic(api_key=settings.api_key, timeout=settings.timeout_seconds,
                                                        max_retries=1)
            except ImportError:
                self.last_error = "anthropic package not installed (pip install -e '.[ai]')"
                log.warning("AI assist enabled but %s", self.last_error)

    @property
    def available(self) -> bool:
        return self._client is not None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.settings.enabled, "available": self.available, "model": self.settings.model,
            "calls_today": self._calls_today, "max_calls_per_day": self.settings.max_calls_per_day,
            "last_error": self.last_error,
        }

    async def classify(self, *, headline: str, summary: str, symbols: list[str], today: date) -> dict[str, Any] | None:
        if self._client is None:
            return None
        if self._day != today:
            self._day, self._calls_today = today, 0
        if self._calls_today >= self.settings.max_calls_per_day:
            return None
        self._calls_today += 1
        prompt = f"Companies (NSE symbols): {', '.join(symbols) or 'none resolved'}\nHeadline: {headline}\nDetails: {summary[:3000]}"
        try:
            response = await self._client.beta.messages.create(
                model=self.settings.model,
                max_tokens=1024,
                system=_SYSTEM,
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA}},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - the AI layer must never break the engine
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("AI assist call failed: %s", self.last_error)
            return None
        if response.stop_reason == "refusal":
            self.last_error = "model declined"
            return None
        text = next((block.text for block in response.content if block.type == "text"), None)
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            self.last_error = "non-JSON response"
            return None
        data["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        data["model"] = response.model
        data["advisory_only"] = True
        return data
