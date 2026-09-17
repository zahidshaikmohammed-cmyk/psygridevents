# PSYGRIDEVENTS — Precision Constitution v1.0

## Mission

Build a real-time event intelligence engine that identifies material developments affecting the monitored universe and explains their possible market transmission with an auditable evidence trail.

## Non-negotiable rules

### 1. Fact before inference
The system must preserve the source statement before generating interpretation.

### 2. Source hierarchy
Primary disclosures and official releases outrank secondary reporting. Social content is discovery material until independently verified.

### 3. Event identity
A story is not an event merely because a new article appeared. The engine must distinguish a new event, an update to an existing event, a correction, a duplicate, and a developing story.

### 4. Time integrity
Publication time, event time, first-seen time and market-confirmation time are separate fields. The engine must never silently substitute one for another.

### 5. Entity integrity
A company mention must be resolved against the canonical instrument universe before it can affect a monitored instrument.

### 6. Exposure is explicit
Every impact claim must identify its exposure path: direct, sector, competitive, supply-chain, commodity, macro or second-order.

### 7. Mechanism is mandatory
A directional interpretation without a plausible economic/operational mechanism is incomplete.

### 8. Surprise matters
Expected information and unexpected information must not receive identical treatment. Expectations must be stored separately from actual outcomes when available.

### 9. Materiality is multidimensional
Importance must consider source quality, novelty, surprise, financial materiality, exposure, market relevance, persistence and transmission. No single sentiment field can determine priority.

### 10. Conflict is first-class data
If fundamental/news interpretation and observed market reaction disagree, the engine must preserve the conflict and investigate it rather than forcing agreement.

### 11. Uncertainty is explicit
Missing evidence, ambiguous wording, conflicting sources and unresolved entity mappings must be represented as uncertainty.

### 12. No fabrication
The engine must never invent an event, source, price, expectation, causal link or historical analogue. Unknown remains unknown.

### 13. No headline-only conclusions
A headline alone cannot establish materiality or market impact when the body/source context is available.

### 14. Deterministic core
Ingestion, validation, normalization, deduplication, canonical identifiers, scoring and persistence must remain testable deterministic components.

### 15. Reasoning is bounded
LLM/semantic reasoning may interpret evidence, but it must operate inside explicit schemas and cannot silently modify source facts.

### 16. Evidence retention
Every material assessment must be traceable to one or more source records and the extracted facts that support it.

### 17. No trade execution
PSYGRIDEVENTS produces intelligence. It does not place orders.

### 18. No silent stale data
Stale, delayed or unavailable sources must be marked as such. The engine must not present old information as current.

### 19. Ranking is explainable
If an event is ranked above another event, the system must retain the component factors responsible for that ordering.

### 20. Version everything important
Event schemas, scoring formulas, source policies, universe definitions and reasoning prompts are versioned artifacts.
