from __future__ import annotations

from collections.abc import Iterable

from .models import Event, IntelligenceRecord
from .scoring import ScoreInputs, calculate_priority


class EventIntelligenceEngine:
    """Orchestrates deterministic assessment of already-extracted events.

    Source adapters and semantic reasoning are intentionally injected later.
    This keeps the core engine testable and prevents external services from
    silently becoming part of the truth layer.
    """

    def assess(self, event: Event, inputs: ScoreInputs) -> IntelligenceRecord:
        assessment = calculate_priority(event, inputs)
        return IntelligenceRecord(event=event, assessment=assessment)

    def rank(self, records: Iterable[IntelligenceRecord]) -> list[IntelligenceRecord]:
        """Rank by materiality while preserving deterministic tie-breaking."""
        return sorted(
            records,
            key=lambda record: (
                record.assessment.priority_score,
                record.assessment.source_confidence,
                record.event.event_id,
            ),
            reverse=True,
        )
