"""LiveEngine: the deterministic core of the production service.

ACQUISITION (runtime) -> NORMALIZATION -> DEDUPLICATION -> STORY FUSION ->
ENTITY RESOLUTION -> EVENT CLASSIFICATION -> EVENT EXTRACTION -> MATERIALITY ->
NOVELTY -> DIRECTION -> EXPOSURE -> TRANSMISSION -> LIVE MARKET CONFIRMATION ->
RELATIVE STRENGTH -> VOLUME -> VWAP/PRICE STRUCTURE -> EXHAUSTION ->
OPPORTUNITY SCORE -> CP11 SIGNAL STATE -> STATEFUL SIGNAL BOOK -> RANKING -> DELIVERY

This class has no network I/O and no clock of its own: observations, market
snapshots and `now` are passed in, so the full pipeline is deterministic and
testable with fixtures. The async runtime (runtime.py) feeds it.
"""
from __future__ import annotations

import logging
from collections import Counter, deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from .acquisition import RawObservation
from .asset_mechanism import AssetMechanismMapping
from .contradiction import ContradictionEngine
from .direction import DirectionEngine
from .entity_resolution import EntityResolver
from .event_classifier import EventClassifier
from .event_pipeline import CanonicalEvent, EventBuilder, observation_id
from .event_timing import EventTimingEngine
from .exhaustion import ExhaustionEngine
from .exposure_graph import Exposure, ExposureGraph990
from .fusion import Evidence, Story, StoryBook, content_key, url_key
from .market_calendar import TRADING_PHASES, MarketCalendar, SessionPhase
from .market_response import MarketResponseEngine
from .market_snapshot import MarketSnapshot, volume_profile
from .materiality import MaterialityEngine
from .normalize import normalized_observation
from .novelty import NoveltyEngine
from .opportunity import OpportunityScore, ScoreContext, score_opportunity
from .outcomes import OutcomeTracker
from .reaction import ReactionAnalyzer, ReactionMetrics
from .semantic import Magnitude, SemanticEvent, SemanticExtractor
from .settings import CONFIG_DIR, Settings
from .signal_book import (
    ACTIONABLE,
    CONFIRMED,
    EARLY_LONG,
    EARLY_SHORT,
    EXHAUSTED,
    INVALIDATED,
    NO_SIGNAL,
    TERMINAL,
    WATCH,
    RankedEntry,
    SignalBook,
    TopRanker,
    Transition,
)
from .signal_engine import SignalEngine
from .storage import Store, parse_iso
from .universe import load_instruments

log = logging.getLogger("psygridevents.engine")

MAX_EXPOSURES_EVALUATED = 40


@dataclass
class EvaluationResult:
    at: datetime
    phase: str
    top: list[RankedEntry]
    transitions: list[Transition]
    alerts: list[Transition]
    candidates: int
    market_health: str
    counts: dict[str, int] = field(default_factory=dict)


def _label_strength(value: float | None, direction_sign: float) -> str:
    if value is None or not direction_sign:
        return "Unavailable"
    aligned = value * direction_sign
    if aligned >= 0.015:
        return "Strong"
    if aligned >= 0.005:
        return "Moderate"
    if aligned > -0.005:
        return "Neutral"
    return "Negative"


def _fmt_age(seconds: float | None) -> str | None:
    if seconds is None:
        return None
    seconds = int(max(0, seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}h {minutes}m {secs}s" if hours else f"{minutes}m {secs}s"


class LiveEngine:
    def __init__(self, settings: Settings, store: Store, *, universe: Iterable[str] | None = None,
                 calendar: MarketCalendar | None = None) -> None:
        self.settings = settings
        self.store = store
        self.universe = tuple(universe) if universe is not None else load_instruments(settings.instruments_file)
        self.universe_set = frozenset(self.universe)
        self.calendar = calendar or MarketCalendar.load(settings.session)
        self.classifier = EventClassifier()
        self.extractor = SemanticExtractor(CONFIG_DIR / "semantic_rules.yaml")
        self.direction_engine = DirectionEngine(CONFIG_DIR / "direction_rules.yaml")
        self.materiality_engine = MaterialityEngine(CONFIG_DIR / "materiality_rules.yaml")
        self.novelty_engine = NoveltyEngine()
        self.contradiction_engine = ContradictionEngine(CONFIG_DIR / "contradiction_rules.yaml")
        self.graph = ExposureGraph990(self.universe)
        self.timing_engine = EventTimingEngine(CONFIG_DIR / "event_timing_rules.yaml")
        self.exhaustion_engine = ExhaustionEngine()
        self.cp11 = SignalEngine()
        self.analyzer = ReactionAnalyzer(MarketResponseEngine(CONFIG_DIR / "market_response_rules.yaml"))
        self.issuer_names: dict[str, str] = {}
        self.resolver: EntityResolver
        self.builder: EventBuilder
        self.reload_issuers()
        self.storybook = StoryBook(window_hours=settings.story_window_hours)
        self.events: dict[str, CanonicalEvent] = {}
        self.history: dict[str, SemanticEvent] = {}
        self.seen_keys: set[str] = set()
        self.book = SignalBook(store, cooldown_minutes=settings.signals.alert_cooldown_minutes,
                               dwell_minutes=settings.signals.min_state_dwell_minutes)
        self.ranker = TopRanker(
            top_n=settings.signals.top_n, min_score=settings.signals.min_opportunity_score,
            swap_margin=settings.signals.rank_swap_margin, multiple_event_bonus=settings.signals.multiple_event_bonus,
        )
        self.outcomes = OutcomeTracker(store)
        self.top: list[RankedEntry] = []
        self.last_evaluation: EvaluationResult | None = None
        self.last_reactions: dict[str, ReactionMetrics] = {}
        self.direction_by_pair: dict[tuple[str, str], str] = {}
        self.volume_history: dict[str, list[dict[str, float]]] = {}
        self.volume_history_date: str | None = None
        self.counters: Counter[str] = Counter()
        self.ai_queue: deque[str] = deque(maxlen=200)
        self.current_trade_date: str | None = None

    # ------------------------------------------------------------- reference data
    def reload_issuers(self) -> None:
        issuers = self.store.issuers()
        self.issuer_names = {symbol: row["company_name"] for symbol, row in issuers.items() if row.get("company_name")}
        self.resolver = EntityResolver.from_files(self.universe, CONFIG_DIR / "issuer_aliases.yaml", self.issuer_names)
        self.builder = EventBuilder(
            resolver=self.resolver, classifier=self.classifier, extractor=self.extractor,
            direction_engine=self.direction_engine, materiality_engine=self.materiality_engine,
            novelty_engine=self.novelty_engine, contradiction_engine=self.contradiction_engine, graph=self.graph,
            calendar=self.calendar,
        )

    def company(self, symbol: str) -> str | None:
        if symbol in self.issuer_names:
            return self.issuer_names[symbol]
        return None

    def load_volume_history(self, trade_date: str) -> None:
        if self.volume_history_date == trade_date:
            return
        self.volume_history = self.store.volume_profiles(before_date=trade_date, days=20)
        self.volume_history_date = trade_date

    # ------------------------------------------------------------- restart recovery
    def restore(self, now: datetime) -> dict[str, int]:
        trade_date = self.calendar.trade_date(now).isoformat()
        self.current_trade_date = trade_date
        since = now - timedelta(hours=self.settings.story_window_hours)
        recent = self.store.recent_observations(now - timedelta(days=3), limit=20000)
        for row in recent:
            self.seen_keys.add(row["content_key"])
            self.seen_keys.add(url_key(row["url"]))
        restored_stories: list[Story] = []
        for row in self.store.stories(since=since, limit=2000):
            evidence: list[Evidence] = []
            for obs in self.store.observations_for_story(row["story_id"]):
                observation = RawObservation(
                    provider_id=obs["provider_id"], source_tier=int(obs["source_tier"]), publisher=obs["publisher"],
                    title=obs["title"], url=obs["url"], summary=obs.get("summary") or "",
                    published_at=parse_iso(obs.get("published_at")), observed_at=parse_iso(obs["observed_at"]) or now,
                    raw=obs.get("raw") or {}, source_quality=obs["source_quality"],
                )
                evidence.append(self.builder.evidence_from(observation, obs_id=obs["obs_id"]))
            if evidence:
                restored_stories.append(Story(row["story_id"], evidence, list(row.get("contradictions") or [])))
        self.storybook.restore(restored_stories)
        for row in self.store.events(since=now - timedelta(hours=self.settings.recent_event_memory_hours), limit=5000):
            semantic = _semantic_from_payload(row)
            if semantic is not None:
                self.history[row["event_id"]] = semantic
        for story in restored_stories:
            event = self.builder.build(story, history=tuple(self.history.values()), now=now)
            self.events[event.event_id] = event
            self.history[event.event_id] = event.semantic_event
        signals = self.book.restore(trade_date)
        for record in self.book.active():
            self.direction_by_pair[(record.event_id, record.symbol)] = record.direction
        outcomes = self.outcomes.restore()
        self.ranker.previous = list(self.store.get_kv("top_symbols", []) or [])
        counts = {"stories": len(restored_stories), "events": len(self.events), "signals": signals,
                  "open_outcomes": outcomes, "seen_keys": len(self.seen_keys)}
        log.info("restored state after restart: %s", counts)
        return counts

    # ------------------------------------------------------------- ingestion
    def ingest(self, observations: Iterable[RawObservation], now: datetime) -> list[CanonicalEvent]:
        touched: dict[str, Story] = {}
        for observation in observations:
            observation = normalized_observation(observation)
            if not observation.title or not observation.url:
                self.counters["observations_rejected_incomplete"] += 1
                continue
            ck = content_key(observation)
            uk = url_key(observation.url)
            if ck in self.seen_keys or uk in self.seen_keys:
                self.counters["observations_duplicate"] += 1
                continue
            self.seen_keys.update((ck, uk))
            obs_id = observation_id(observation)
            evidence = self.builder.evidence_from(observation, obs_id=obs_id)
            story, created = self.storybook.add(evidence)
            self.counters["observations_new"] += 1
            self.counters["stories_created" if created else "observations_fused"] += 1
            self.store.insert_observation({
                "obs_id": obs_id, "provider_id": observation.provider_id,
                "source_quality": evidence.quality, "source_tier": observation.source_tier,
                "publisher": observation.publisher, "title": observation.title, "url": observation.url,
                "summary": observation.summary, "published_at": observation.published_at,
                "observed_at": observation.observed_at, "ingestion_latency_seconds": evidence.ingestion_latency_seconds,
                "content_key": ck, "story_id": story.story_id, "raw": observation.raw,
            })
            touched[story.story_id] = story
        updated: list[CanonicalEvent] = []
        for story in touched.values():
            event = self.builder.build(story, history=tuple(self.history.values()), now=now)
            self.events[event.event_id] = event
            self.history[event.event_id] = event.semantic_event
            self._persist_event(story, event)
            story.dirty = False
            updated.append(event)
            if (
                event.direction == "unknown" and event.materiality_score >= self.settings.ai.min_materiality_score
                and event.confirmation_status == "CONFIRMED_PRIMARY" and not event.routine
            ):
                self.ai_queue.append(event.event_id)
        self._trim_memory(now)
        return updated

    def _persist_event(self, story: Story, event: CanonicalEvent) -> None:
        self.store.upsert_story({
            "story_id": story.story_id, "title": event.headline, "first_seen": story.first_seen,
            "latest_seen": story.latest_seen, "first_public_at": story.first_public_at,
            "source_count": len(story.evidence), "publisher_count": len(story.publishers),
            "best_quality": story.best_quality, "confirmation_status": story.confirmation_status,
            "symbols": sorted(story.symbols), "event_type": event.event_type, "contradictions": event.contradictions,
            "payload": {"event_id": event.event_id, "publishers": story.publishers},
        })
        record = event.to_record()
        record["payload"]["semantic_min"] = {
            "trigger": event.semantic_event.trigger, "instruments": list(event.semantic_event.instruments),
            "participants": list(event.semantic_event.participants), "event_type": event.event_type,
            "event_time": event.public_at.isoformat(), "magnitude_text": event.magnitude_text,
            "direct_effect": event.semantic_event.direct_effect, "headline": event.headline,
            "story_id": event.story_id,
        }
        self.store.upsert_event(record)
        self.store.replace_exposures(event.event_id, [
            {**item.to_dict(), "payload": {"basis": item.basis, "group": item.group, "source_symbol": item.source_symbol}}
            for item in event.exposures
        ])

    def _trim_memory(self, now: datetime) -> None:
        for story_id in self.storybook.expire(now):
            self.events.pop("ev-" + story_id[3:], None)
        cutoff = now - timedelta(hours=self.settings.recent_event_memory_hours)
        for event_id, semantic in list(self.history.items()):
            if semantic.event_time and semantic.event_time < cutoff:
                del self.history[event_id]
        if len(self.seen_keys) > 400_000:
            self.seen_keys = set(list(self.seen_keys)[-200_000:])

    def apply_ai_assist(self, event_id: str, assist: dict[str, Any]) -> CanonicalEvent | None:
        """Attach an optional AI suggestion. It can only fill an UNKNOWN direction, at capped confidence."""
        event = self.events.get(event_id)
        if event is None:
            return None
        direction = assist.get("direction")
        updated = replace(event, ai_assist=assist)
        if event.direction == "unknown" and direction in ("positive", "negative"):
            confidence = min(0.45, float(assist.get("confidence", 0.0)) * 0.5)
            exposures = [
                replace(item, expected_direction=direction) if item.relationship == "DIRECT" else item
                for item in event.exposures
            ]
            updated = replace(updated, direction=direction, direction_confidence=round(confidence, 3),
                              direction_basis="ai_assist(advisory)", exposures=exposures)
        self.events[event_id] = updated
        return updated

    # ------------------------------------------------------------- evaluation
    def evaluate(self, snapshot: MarketSnapshot | None, now: datetime) -> EvaluationResult:
        phase = self.calendar.phase(now)
        trade_date = self.calendar.trade_date(now).isoformat()
        if self.current_trade_date != trade_date:
            self.book.roll_day(trade_date)
            self.current_trade_date = trade_date
            self.ranker.previous = []
        self.load_volume_history(trade_date)
        regime = snapshot.regime() if snapshot is not None else {"label": "UNKNOWN"}
        market_health = snapshot.health() if snapshot is not None else "DISCONNECTED"
        open_epoch = self.calendar.market_open_at(self.calendar.trade_date(now)).timestamp()
        close_epoch = self.calendar.market_close_at(self.calendar.trade_date(now)).timestamp()
        transitions: list[Transition] = []
        candidates = 0
        max_age = timedelta(hours=self.settings.signals.max_candidate_age_hours)
        self.last_reactions = {}

        for event in list(self.events.values()):
            if event.routine or event.roundup:
                continue
            relation = self.calendar.event_session_relation(event.public_at, now)
            if now - event.public_at > max_age and relation != "BEFORE_OPEN":
                # Events published since the previous close (overnight, weekend, holiday)
                # stay eligible for today's session regardless of wall-clock age.
                continue
            exposures = [item for item in event.exposures if item.relationship == "DIRECT"]
            exposures += [item for item in event.exposures if item.relationship != "DIRECT"][: MAX_EXPOSURES_EVALUATED - len(exposures)]
            for exposure in exposures:
                candidates += 1
                transition = self._evaluate_candidate(
                    event, exposure, snapshot, now=now, phase=phase, regime=regime.get("label", "UNKNOWN"),
                    trade_date=trade_date, open_epoch=open_epoch, close_epoch=close_epoch,
                )
                transitions.extend(transition)

        self.top = self.ranker.rank(self.book.active())
        self.store.set_kv("top_symbols", [entry.symbol for entry in self.top])
        session_closed = phase in (SessionPhase.POST_MARKET, SessionPhase.CLOSED, SessionPhase.NON_TRADING_DAY, SessionPhase.OVERNIGHT)
        if snapshot is not None:
            completed = self.outcomes.update(snapshot, now=now, session_closed=session_closed)
            self.counters["outcomes_completed"] += completed
        alerts = [item for item in transitions if item.alert]
        result = EvaluationResult(
            at=now, phase=phase.value, top=self.top, transitions=transitions, alerts=alerts, candidates=candidates,
            market_health=market_health,
            counts={state: sum(1 for record in self.book.active() if record.state == state) for state in
                    (NO_SIGNAL, WATCH, EARLY_LONG, EARLY_SHORT, CONFIRMED, INVALIDATED, EXHAUSTED)},
        )
        self.last_evaluation = result
        return result

    def _evaluate_candidate(
        self, event: CanonicalEvent, exposure: Exposure, snapshot: MarketSnapshot | None, *, now: datetime,
        phase: SessionPhase, regime: str, trade_date: str, open_epoch: float, close_epoch: float,
    ) -> list[Transition]:
        symbol = exposure.symbol
        documented = exposure.expected_direction if exposure.expected_direction in ("positive", "negative") else "unknown"
        reaction: ReactionMetrics | None = None
        relation = self.calendar.event_session_relation(event.public_at, now)
        if snapshot is not None and symbol in snapshot.series:
            reaction = self.analyzer.analyze(
                snapshot, symbol, public_at=event.public_at, session_relation=relation,
                expected_direction=documented, market_open_epoch=open_epoch, market_close_epoch=close_epoch,
                historical_profiles=self.volume_history.get(symbol),
            )
            self.last_reactions[f"{event.event_id}:{symbol}"] = reaction

        direction = documented
        direction_confidence = event.direction_confidence if exposure.relationship == "DIRECT" else exposure.confidence
        if direction == "unknown" and reaction is not None and reaction.direction_source == "market_reaction" and (
            exposure.relationship == "DIRECT" and event.materiality_score >= 0.45
        ):
            direction = "positive" if reaction.reaction_direction == "up" else "negative"
            direction_confidence = 0.35
        signal_key = f"{event.event_id}:{symbol}:{direction}"

        transitions: list[Transition] = []
        previous_direction = self.direction_by_pair.get((event.event_id, symbol))
        if previous_direction and previous_direction != direction:
            old_key = f"{event.event_id}:{symbol}:{previous_direction}"
            old = self.book.get(old_key)
            if old is not None and old.state not in TERMINAL and old.state in ACTIONABLE | {WATCH}:
                flip = self.book.update(
                    signal_key=old_key, symbol=symbol, event_id=event.event_id, story_id=event.story_id,
                    direction=previous_direction, proposed_state=INVALIDATED, score=0.0,
                    reason=f"Market-implied direction flipped from {previous_direction} to {direction}.",
                    payload=old.payload, trade_date=trade_date, now=now,
                )
                if flip:
                    transitions.append(flip)
                    self.outcomes.note_state(old_key, INVALIDATED)
        self.direction_by_pair[(event.event_id, symbol)] = direction

        # Market-available clock: an event published outside market hours can only be
        # reacted to from the next open, so freshness/timing is measured from there.
        market_available_at = event.public_at
        if relation == "BEFORE_OPEN":
            market_available_at = max(event.public_at, datetime.fromtimestamp(open_epoch, tz=timezone.utc))
        timing_event = replace(event.semantic_event, event_time=market_available_at)
        timing = self.timing_engine.assess(timing_event, as_of=now)
        response = reaction.to_market_response(event.event_id, direction) if reaction is not None else None
        exhaustion = self.exhaustion_engine.assess(timing, response)
        confirmation = reaction.to_confirmation(event.event_id, direction) if reaction is not None else None
        materiality = exposure.materiality if exposure.hop else event.materiality_score
        semantic = replace(
            event.semantic_event,
            instruments=tuple(dict.fromkeys(event.semantic_event.instruments + (symbol,))),
            materiality_score=materiality,
            materiality_status="high" if materiality >= 0.7 else "medium" if materiality >= 0.45 else "low",
        )
        mapping = AssetMechanismMapping(
            event_id=event.event_id, asset=symbol, asset_type="instrument", exposure_type=exposure.relationship.lower(),
            mechanism=exposure.mechanism, expected_direction=direction, confidence=exposure.confidence, evidence=(),
            uncertainty=(), resolved=True, basis=exposure.basis,
        )
        market_state = "MARKET_OPEN" if phase in TRADING_PHASES else "MARKET_CLOSED"
        if snapshot is None or (reaction is not None and reaction.data_status in ("NO_DATA", "TIME_ERROR")):
            market_state = "MARKET_DATA_UNAVAILABLE"
        cp11 = self.cp11.assess(
            semantic, story_id=event.story_id, asset_mapping=mapping, timing=timing, response=response,
            exhaustion=exhaustion, confirmation=confirmation, as_of=now, market_session_state=market_state,
        )
        age_hours = max(0.0, (now - market_available_at).total_seconds() / 3600.0)
        score = score_opportunity(
            event, exposure, reaction, direction=direction, direction_confidence=direction_confidence,
            context=ScoreContext(phase=phase, regime=regime, event_age_hours=age_hours, settings=self.settings.signals),
        )
        current = self.book.get(signal_key)
        proposed, policy_notes = self._policy_state(cp11.signal_state, event, exposure, reaction, score, phase, direction,
                                                    current.state if current else NO_SIGNAL)
        payload = self._signal_payload(event, exposure, reaction, score, cp11, proposed, direction, direction_confidence,
                                       now, policy_notes, timing.state, market_available_at)
        transition = self.book.update(
            signal_key=signal_key, symbol=symbol, event_id=event.event_id, story_id=event.story_id,
            direction=direction, proposed_state=proposed, score=score.opportunity_score,
            reason="; ".join([cp11.trigger] + policy_notes)[:900], payload=payload, trade_date=trade_date, now=now,
        )
        if transition:
            transitions.append(transition)
            self.outcomes.note_state(signal_key, transition.to_state)
            if transition.to_state in ACTIONABLE and reaction is not None and reaction.last_price:
                invalidation = payload.get("invalidation_price")
                self.outcomes.start(
                    outcome_id=f"sig:{signal_key}", signal_key=signal_key, event_id=event.event_id, symbol=symbol,
                    event_type=event.event_type, direction=direction, reference_kind="signal_actionable",
                    reference_at=reaction.latest_bar_at + timedelta(seconds=60) if reaction.latest_bar_at else now,
                    reference_price=reaction.last_price, state=transition.to_state, score=score.opportunity_score,
                    actionable=True, market_state=self._market_state(reaction, snapshot), invalidation_price=invalidation,
                    trade_date=trade_date,
                )
        # Event-level outcome (missed/late signal analysis) for direct, material events first seen in-session.
        if (
            exposure.relationship == "DIRECT" and reaction is not None and reaction.last_price and phase in TRADING_PHASES
            and event.materiality_score >= 0.45 and reaction.data_status == "LIVE" and direction in ("positive", "negative")
        ):
            self.outcomes.start(
                outcome_id=f"evt:{event.event_id}:{symbol}", signal_key=signal_key, event_id=event.event_id, symbol=symbol,
                event_type=event.event_type, direction=direction, reference_kind="event_first_live_evaluation",
                reference_at=reaction.latest_bar_at + timedelta(seconds=60) if reaction.latest_bar_at else now,
                reference_price=reaction.last_price, state=proposed, score=score.opportunity_score, actionable=False,
                market_state=self._market_state(reaction, snapshot), invalidation_price=reaction.baseline_price,
                trade_date=trade_date,
            )
        return transitions

    def _policy_state(
        self, cp11_state: str, event: CanonicalEvent, exposure: Exposure, reaction: ReactionMetrics | None,
        score: OpportunityScore, phase: SessionPhase, direction: str, current_state: str,
    ) -> tuple[str, list[str]]:
        """Production policy layered on the CP11 decision. Only ever makes a signal MORE conservative."""
        notes: list[str] = []
        state = cp11_state
        settings = self.settings.signals
        if event.routine or event.roundup:
            return NO_SIGNAL, ["routine or roundup item"]
        if state in TERMINAL:
            return state, notes
        if state in ACTIONABLE:
            if phase not in TRADING_PHASES:
                state = WATCH
                notes.append(f"session phase {phase.value}: no live entries")
            elif reaction is None or reaction.data_status != "LIVE":
                state = WATCH
                notes.append("market data not LIVE: fail closed")
            elif exposure.hop >= 1 and reaction.confirmation_status != "confirmed":
                state = WATCH
                notes.append("indirect exposure needs its own confirmed market reaction")
            elif event.confirmation_status in ("UNCONFIRMED_DISCOVERY", "CONTRADICTED"):
                if reaction.confirmation_status == "confirmed" and state == CONFIRMED:
                    state = EARLY_LONG if direction == "positive" else EARLY_SHORT
                    notes.append("discovery-only/contradicted evidence capped at EARLY despite market confirmation")
                elif reaction.confirmation_status != "confirmed":
                    state = WATCH
                    notes.append("discovery-only/contradicted evidence without market confirmation")
            if state == CONFIRMED and reaction is not None and reaction.direction_source == "market_reaction":
                state = EARLY_LONG if direction == "positive" else EARLY_SHORT
                notes.append("direction inferred from the reaction itself: capped at EARLY")
            if state in ACTIONABLE and score.opportunity_score < settings.min_opportunity_score:
                notes.append(f"opportunity_score {score.opportunity_score:.1f} below threshold {settings.min_opportunity_score}")
                state = WATCH
            if state in ACTIONABLE and phase == SessionPhase.NEAR_CLOSE and current_state not in ACTIONABLE:
                notes.append("near close: no new entries")
                state = WATCH
        if state == WATCH and score.opportunity_score < settings.watch_min_score and event.materiality_score < 0.45:
            state = NO_SIGNAL
            notes.append("low score and low materiality")
        return state, notes

    def _market_state(self, reaction: ReactionMetrics, snapshot: MarketSnapshot | None) -> dict[str, Any]:
        regime = snapshot.regime() if snapshot is not None else {}
        return {
            "regime": regime.get("label"), "breadth": regime.get("breadth"), "india_vix": regime.get("india_vix"),
            "day_change": reaction.day_change, "move_since_event": reaction.move_since_event,
            "volume_ratio": reaction.volume_ratio, "rvol_historical": reaction.relative_volume_historical,
            "relative_strength": reaction.relative_strength, "exhaustion_level": reaction.exhaustion_level,
            "baseline_price": reaction.baseline_price,
        }

    def _signal_payload(
        self, event: CanonicalEvent, exposure: Exposure, reaction: ReactionMetrics | None, score: OpportunityScore,
        cp11: Any, state: str, direction: str, direction_confidence: float, now: datetime, notes: list[str],
        timing_state: str, market_available_at: datetime | None = None,
    ) -> dict[str, Any]:
        sign = 1.0 if direction == "positive" else -1.0 if direction == "negative" else 0.0
        trade_direction = "LONG" if sign > 0 else "SHORT" if sign < 0 else "NONE"
        event_age = (now - event.public_at).total_seconds()
        market_available_at = market_available_at or event.public_at
        market_available_age = max(0.0, (now - market_available_at).total_seconds())
        market_age = reaction.market_data_age_seconds if reaction else None
        if reaction is None or reaction.data_status in ("NO_DATA", "TIME_ERROR"):
            freshness_status = "NO_MARKET_DATA"
        elif reaction.data_status == "STALE":
            freshness_status = "STALE"
        elif market_available_age <= 1800:
            freshness_status = "FRESH"
        elif market_available_age <= 4 * 3600:
            freshness_status = "AGING"
        else:
            freshness_status = "OLD"

        invalidation_price = None
        invalidation_text = cp11.invalidation
        if reaction is not None and reaction.baseline_price and sign:
            levels = [reaction.baseline_price]
            if reaction.vwap and reaction.vwap_aligned and state == CONFIRMED:
                levels.append(reaction.vwap)
            invalidation_price = round(max(levels) if sign > 0 else min(levels), 4)
            invalidation_text = (
                f"{'Below' if sign > 0 else 'Above'} {invalidation_price:.2f} "
                f"({'event baseline' if len(levels) == 1 else 'max(event baseline, VWAP)' if sign > 0 else 'min(event baseline, VWAP)'}); "
                "or a contradicting primary-source update; or exhaustion (retracement >= 40% from peak)."
            )

        why_parts: list[str] = []
        if event.materiality_status in ("high", "medium"):
            magnitude = f", ₹{event.magnitude_crore:,.0f} cr" if event.magnitude_crore else ""
            why_parts.append(f"{'Fresh ' if event.novelty_status == 'new' else ''}{event.materiality_status} materiality "
                             f"{event.subtype.replace('_', ' ')}{magnitude}")
        if reaction is not None and reaction.move_since_event is not None and sign:
            if reaction.aligned_move and reaction.aligned_move > 0:
                why_parts.append(f"price confirmation {reaction.move_since_event:+.2%} since event")
            volume = max(reaction.volume_ratio or 0, reaction.relative_volume_historical or 0)
            if volume >= 1.5:
                why_parts.append(f"abnormal volume {volume:.1f}x baseline")
            if reaction.relative_strength is not None and reaction.relative_strength * sign > 0.005:
                why_parts.append(f"relative strength {reaction.relative_strength:+.2%} vs {reaction.relative_strength_basis}")
            if reaction.vwap_aligned:
                why_parts.append(f"{'above' if sign > 0 else 'below'} VWAP")
            if reaction.exhaustion_level in ("LOW", "MEDIUM"):
                why_parts.append(f"exhaustion {reaction.exhaustion_level.lower()}")
        why_now = " + ".join(why_parts) if why_parts else "; ".join(score.why) or cp11.trigger

        volume_ratio = None
        if reaction is not None:
            volume_ratio = reaction.volume_ratio if reaction.volume_ratio is not None else reaction.relative_volume_historical
        return {
            "signal_key": f"{event.event_id}:{exposure.symbol}:{direction}",
            "rank": None,
            "symbol": exposure.symbol,
            "company": self.company(exposure.symbol),
            "signal_state": state,
            "cp11_state": cp11.signal_state,
            "direction": trade_direction,
            "direction_basis": (
                "market_reaction" if reaction is not None and reaction.direction_source == "market_reaction"
                and exposure.expected_direction not in ("positive", "negative") else event.direction_basis
                if exposure.relationship == "DIRECT" else f"exposure:{exposure.basis}"
            ),
            "direction_confidence": round(direction_confidence, 3),
            "opportunity_score": score.opportunity_score,
            "score_is_probability": False,
            "event": {
                "event_id": event.event_id, "story_id": event.story_id, "headline": event.headline,
                "type": event.event_type, "subtype": event.subtype, "magnitude_crore": event.magnitude_crore,
                "materiality": event.materiality_status, "materiality_score": event.materiality_score,
                "novelty": event.novelty_status, "confirmation_status": event.confirmation_status,
                "contradiction_status": event.contradiction_status,
            },
            "event_time": event.public_at.isoformat(),
            "event_time_basis": event.public_at_basis,
            "source": {
                "best_quality": event.source_quality, "publishers": list(event.publishers[:5]),
                "source_count": event.source_count,
            },
            "market_reaction": None if reaction is None else {
                "move_since_event": reaction.move_since_event, "baseline_kind": reaction.baseline_kind,
                "baseline_price": reaction.baseline_price, "response_state": reaction.response_state,
                "confirmation": reaction.confirmation_status, "confirmation_reason": reaction.confirmation_reason,
                "reaction_start_at": reaction.reaction_start_at.isoformat() if reaction.reaction_start_at else None,
                "event_to_price_latency_minutes": reaction.event_to_price_latency_minutes,
                "persistence": reaction.persistence, "peak_aligned_move": reaction.peak_aligned_move,
                "retracement_from_peak": reaction.retracement_from_peak, "momentum_5m": reaction.momentum_5m,
                "momentum_15m": reaction.momentum_15m, "gap": reaction.gap,
            },
            "price": reaction.last_price if reaction else None,
            "intraday_return": reaction.intraday_return if reaction else None,
            "day_change": reaction.day_change if reaction else None,
            "relative_strength": {
                "label": _label_strength(reaction.relative_strength if reaction else None, sign),
                "value": reaction.relative_strength if reaction else None,
                "basis": reaction.relative_strength_basis if reaction else None,
                "sector": reaction.sector if reaction else None,
                "sector_relative": reaction.sector_relative_strength if reaction else None,
            },
            "volume_confirmation": {
                "ratio": volume_ratio,
                "basis": None if reaction is None else (
                    "post_event_vs_pre_event_median_1m" if reaction.volume_ratio is not None
                    else "cumulative_vs_historical_profile" if reaction.relative_volume_historical is not None else None),
                "confirmed": bool(volume_ratio and volume_ratio >= 1.5),
            },
            "VWAP_state": reaction.vwap_state if reaction else "unavailable",
            "vwap": reaction.vwap if reaction else None,
            "exhaustion_state": reaction.exhaustion_level if reaction else "UNKNOWN",
            "exhaustion_flags": list(reaction.exhaustion_flags) if reaction else [],
            "remaining_opportunity": reaction.remaining_opportunity if reaction else None,
            "clocks": {
                "event_clock": {"public_at": event.public_at.isoformat(), "basis": event.public_at_basis,
                                "timing_state": timing_state, "session_relation": self.calendar.event_session_relation(event.public_at, now)},
                "market_clock": {
                    "reaction_start_at": reaction.reaction_start_at.isoformat() if reaction and reaction.reaction_start_at else None,
                    "minutes_since_reaction_start": reaction.minutes_since_reaction_start if reaction else None,
                    "event_to_price_latency_minutes": reaction.event_to_price_latency_minutes if reaction else None,
                },
                "opportunity_clock": {
                    "remaining_opportunity": reaction.remaining_opportunity if reaction else None,
                    "exhaustion_level": reaction.exhaustion_level if reaction else "UNKNOWN",
                },
            },
            "freshness": {
                "source_timestamp": event.public_at.isoformat(),
                "observed_timestamp": event.first_seen.isoformat(),
                "ingestion_latency_seconds": event.ingestion_latency_seconds,
                "market_data_age_seconds": market_age,
                "event_age_seconds": round(event_age, 1),
                "event_age": _fmt_age(event_age),
                "market_available_at": market_available_at.isoformat(),
                "age_since_market_available": _fmt_age(market_available_age),
                "freshness_status": freshness_status,
            },
            "mechanism": {
                "relationship": exposure.relationship, "mechanism": exposure.mechanism, "hop": exposure.hop,
                "confidence": exposure.confidence, "basis": exposure.basis, "source_symbol": exposure.source_symbol,
            },
            "why_now": why_now,
            "why": list(score.why),
            "invalidation": invalidation_text,
            "invalidation_price": invalidation_price,
            "risk_flags": list(score.risk_flags),
            "source_urls": list(event.source_urls),
            "score_breakdown": score.to_dict(),
            "policy_notes": notes,
            "cp11_trigger": cp11.trigger,
            "uncertainty": list(dict.fromkeys(list(event.uncertainty) + (list(reaction.uncertainty) if reaction else [])))[:12],
            "evaluated_at": now.isoformat(),
        }

    # ------------------------------------------------------------- end of day
    def end_of_day(self, snapshot: MarketSnapshot | None, now: datetime) -> dict[str, Any]:
        trade_date = self.calendar.trade_date(now)
        day = trade_date.isoformat()
        if snapshot is not None:
            open_epoch = self.calendar.market_open_at(trade_date).timestamp()
            profiles = {}
            for symbol, series in snapshot.series.items():
                if len(series) >= 30:
                    profiles[symbol] = volume_profile(series, market_open_epoch=open_epoch)
            if profiles:
                self.store.save_volume_profiles(day, profiles)
            self.outcomes.update(snapshot, now=now, session_closed=True)
        signals = self.store.load_signals(active_only=False, trade_date=day)
        outcomes = self.store.outcomes(trade_date=day, limit=5000)
        states = Counter(item.state for item in signals)
        actionable = [item for item in signals if item.first_actionable_at is not None]
        invalidated_after_actionable = [item.symbol for item in actionable if item.state == INVALIDATED]
        late = []
        missed = []
        for outcome in outcomes:
            if outcome["reference_kind"] == "event_first_live_evaluation" and not outcome["actionable"]:
                peak = outcome.get("mfe") or 0.0
                if peak >= 0.02 and not any(item.signal_key == outcome["signal_key"] and item.first_actionable_at for item in signals):
                    missed.append({"symbol": outcome["symbol"], "event_id": outcome["event_id"], "mfe": peak})
            if outcome["reference_kind"] == "signal_actionable":
                move_at_ref = outcome["market_state"].get("move_since_event")
                if move_at_ref is not None and outcome.get("mfe") is not None:
                    total = abs(move_at_ref) + max(0.0, outcome["mfe"])
                    if total > 0 and abs(move_at_ref) / total > 0.6:
                        late.append({"symbol": outcome["symbol"], "signal_key": outcome["signal_key"],
                                     "share_of_move_before_signal": round(abs(move_at_ref) / total, 3)})
        events_today = [item for item in self.store.events(since=self.calendar.at(trade_date, self.settings.session.pre_market_start) - timedelta(hours=16), limit=5000)]
        diagnostics = {
            "trade_date": day,
            "generated_at": now.isoformat(),
            "events": len(events_today),
            "events_by_type": dict(Counter(item["event_type"] for item in events_today)),
            "events_by_quality": dict(Counter(item["source_quality"] for item in events_today)),
            "signals_by_state": dict(states),
            "actionable_signals": len(actionable),
            "false_signals_invalidated_after_actionable": invalidated_after_actionable,
            "late_signals": late,
            "missed_signals": missed,
            "outcomes_recorded": len(outcomes),
            "counters": dict(self.counters),
            "note": "Diagnostics are descriptive. No hit-rate or probability is implied.",
        }
        self.store.save_daily_diagnostics(day, diagnostics)
        return diagnostics


def _semantic_from_payload(row: dict[str, Any]) -> SemanticEvent | None:
    payload = row.get("payload") or {}
    minimal = payload.get("semantic_min")
    if not minimal:
        return None
    event_time = parse_iso(minimal.get("event_time"))
    magnitude = None
    if minimal.get("magnitude_text"):
        magnitude = Magnitude(minimal["magnitude_text"], "", None, None, minimal["magnitude_text"])
    from .semantic import EvidenceSpan

    return SemanticEvent(
        event_id=row["event_id"], story_id=minimal.get("story_id") or row.get("story_id", ""),
        event_type=minimal.get("event_type") or row["event_type"], trigger=minimal.get("trigger") or "",
        event_time=event_time, instruments=tuple(minimal.get("instruments") or ()),
        participants=tuple(minimal.get("participants") or ()), magnitude=magnitude,
        direct_effect=minimal.get("direct_effect"), indirect_effect=None, competitor_effect=None,
        supply_chain_effect=None, time_horizon=None, novelty_status=row.get("novelty_status", "unknown"),
        surprise_status="not_assessed", modality=row.get("modality", "asserted"), negated=bool(row.get("negated")),
        extraction_confidence=0.5,
        evidence=(EvidenceSpan("", "", minimal.get("headline") or "", 3),), uncertainty=(), market_mechanism=None,
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
