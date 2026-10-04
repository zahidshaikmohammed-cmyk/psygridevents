"""Event -> stock -> mechanism -> market reaction -> opportunity -> ranked signal (deterministic)."""
from __future__ import annotations

from datetime import timedelta

from live_support import (
    Bars,
    at,
    background_universe,
    ist,
    make_engine,
    media,
    nse_filing,
    snapshot,
    state_of,
)

from psygridevents.signal_book import ACTIONABLE


def _run(engine, bars, minutes, **kwargs):
    results = []
    for minute in minutes:
        now = at(minute) + timedelta(seconds=10)
        results.append(engine.evaluate(snapshot(now, bars, **kwargs), now))
    return results


def _order_win(engine, minute=60, symbol="LT", company="Larsen & Toubro Limited", value="2,500"):
    when = at(minute)
    obs = nse_filing(symbol, company, "Bagging of Order", when,
                     summary=f"{company} has informed the Exchange regarding bagging of order worth Rs {value} crore")
    events = engine.ingest([obs], when + timedelta(seconds=30))
    return events[0]


def test_fresh_event_during_market_progresses_watch_early_confirmed_and_ranks():
    engine = make_engine()
    event = _order_win(engine)
    assert event.symbols == ("LT",)
    assert event.subtype == "order_win" and event.direction == "positive"
    assert event.session_relation == "DURING_SESSION"
    lt = Bars("LT", 3500).move(60, 120, 0.0012, volume_mult=3.0)
    bars = [lt] + background_universe(engine, exclude={"LT"})
    states = []
    for minute in [61, 62, 64, 66, 70, 80]:
        _run(engine, bars, [minute])
        states.append(state_of(engine, "LT"))
    assert states[0] in ("WATCH", "NO_SIGNAL")
    assert "EARLY_LONG" in states
    assert states[-1] == "CONFIRMED"
    top = engine.top
    assert top and top[0].symbol == "LT"
    payload = top[0].payload
    for field in ("rank", "symbol", "company", "signal_state", "direction", "opportunity_score", "event", "event_time",
                  "source", "market_reaction", "price", "intraday_return", "relative_strength", "volume_confirmation",
                  "VWAP_state", "exhaustion_state", "freshness", "mechanism", "why_now", "invalidation", "risk_flags",
                  "source_urls"):
        assert field in payload, field
    assert payload["direction"] == "LONG"
    assert payload["score_is_probability"] is False
    assert payload["freshness"]["freshness_status"] == "FRESH"
    assert payload["volume_confirmation"]["confirmed"] is True


def test_large_event_without_price_reaction_is_not_actionable():
    engine = make_engine()
    _order_win(engine, value="9,000")
    flat = Bars("LT", 3500)  # no reaction at all
    _run(engine, [flat] + background_universe(engine, exclude={"LT"}), [62, 70, 80, 90])
    assert state_of(engine, "LT") in ("WATCH", "NO_SIGNAL")
    assert not engine.top


def test_small_event_with_strong_reaction_outranks_large_event_without_reaction():
    engine = make_engine()
    _order_win(engine, value="9,000")  # very large, but the market ignores it
    when = at(60)
    small = nse_filing("DIXON", "Dixon Technologies (India) Limited", "Receipt of Order", when,
                       summary="Dixon Technologies has received an order worth Rs 120 crore")
    engine.ingest([small], when + timedelta(seconds=30))
    bars = [Bars("LT", 3500), Bars("DIXON", 15000).move(60, 120, 0.0015, volume_mult=4.0)]
    bars += background_universe(engine, exclude={"LT", "DIXON"})
    _run(engine, bars, [62, 64, 66, 70, 75])
    assert engine.top and engine.top[0].symbol == "DIXON"
    assert all(entry.symbol != "LT" for entry in engine.top)


def test_exhausted_move_becomes_exhausted_and_drops_out():
    engine = make_engine()
    _order_win(engine)
    lt = Bars("LT", 3500).move(60, 70, 0.006, volume_mult=5.0).move(70, 90, -0.0035, volume_mult=1.0)
    _run(engine, [lt] + background_universe(engine, exclude={"LT"}), [62, 64, 66, 70, 75, 80, 85, 90])
    assert state_of(engine, "LT") == "EXHAUSTED"
    assert all(entry.symbol != "LT" for entry in engine.top)


def test_price_moving_against_documented_direction_invalidates():
    engine = make_engine()
    _order_win(engine)
    lt = Bars("LT", 3500).move(60, 80, -0.0015, volume_mult=3.0)
    _run(engine, [lt] + background_universe(engine, exclude={"LT"}), [62, 64, 66, 70, 75])
    assert state_of(engine, "LT") == "INVALIDATED"
    assert not any(entry.symbol == "LT" for entry in engine.top)


def test_event_before_market_uses_previous_close_and_opening_gap():
    engine = make_engine()
    when = ist(8, 30)
    obs = nse_filing("LT", "Larsen & Toubro Limited", "Bagging of Order", when,
                     summary="Larsen & Toubro bags order worth Rs 3,000 crore")
    event = engine.ingest([obs], when + timedelta(seconds=20))[0]
    assert engine.calendar.event_session_relation(event.public_at, at(5)) == "BEFORE_OPEN"
    lt = Bars("LT", 3600, previous_close=3500, today_open=3580).move(0, 30, 0.0008, volume_mult=2.5)
    _run(engine, [lt] + background_universe(engine, exclude={"LT"}), [3, 6, 10, 15])
    reaction = engine.last_reactions[f"{event.event_id}:LT"]
    assert reaction.baseline_kind == "previous_close"
    assert reaction.gap and reaction.gap > 0.02
    assert state_of(engine, "LT") in ACTIONABLE
    # No historical volume profile exists yet: volume is reported unavailable, never invented.
    assert reaction.volume_ratio is None and reaction.relative_volume_historical is None


def test_event_after_close_produces_no_live_signal():
    engine = make_engine()
    when = ist(16, 5)
    obs = nse_filing("LT", "Larsen & Toubro Limited", "Bagging of Order", when,
                     summary="Larsen & Toubro bags order worth Rs 3,000 crore")
    engine.ingest([obs], when + timedelta(seconds=20))
    now = when + timedelta(minutes=5)
    lt = Bars("LT", 3500).move(300, 375, 0.002, volume_mult=3)
    engine.evaluate(snapshot(now, [lt] + background_universe(engine, exclude={"LT"})), now)
    assert state_of(engine, "LT") in ("WATCH", "NO_SIGNAL")
    assert not engine.top


def test_stale_market_data_fails_closed():
    engine = make_engine()
    _order_win(engine)
    lt = Bars("LT", 3500).move(60, 120, 0.0015, volume_mult=4.0)
    bars = [lt] + background_universe(engine, exclude={"LT"})
    # Data frozen at minute 66, engine clock at minute 75 -> STALE.
    frozen = at(66) + timedelta(seconds=10)
    now = at(75)
    snap = snapshot(frozen, bars)
    snap.as_of = now
    engine.evaluate(snap, now)
    assert state_of(engine, "LT") not in ACTIONABLE
    reaction = engine.last_reactions[next(iter(engine.last_reactions))]
    assert reaction.data_status == "STALE"


def test_missing_market_data_and_psygrid_outage_never_fabricate_signals():
    engine = make_engine()
    _order_win(engine)
    engine.evaluate(None, at(70))  # PSYGRID unreachable
    assert state_of(engine, "LT") in ("WATCH", "NO_SIGNAL")
    snap = snapshot(at(70), background_universe(engine, exclude={"LT"}))  # LT absent from feed
    engine.evaluate(snap, at(70))
    assert state_of(engine, "LT") in ("WATCH", "NO_SIGNAL")
    assert not engine.top


def test_zero_volume_is_flagged_and_not_actionable():
    engine = make_engine()
    _order_win(engine)
    lt = Bars("LT", 3500).move(60, 120, 0.0015, volume_mult=3.0)
    bars = background_universe(engine, exclude={"LT"})
    now = at(75)
    snap = snapshot(now, bars, series_overrides={"LT": lt.series(now, zero_volume_from=69)})
    engine.evaluate(snap, now)
    payload = next(record.payload for record in engine.book.records.values() if record.symbol == "LT")
    assert "zero_volume_recent_bars" in payload["risk_flags"]
    assert payload["opportunity_score"] < 60 or payload["signal_state"] not in ACTIONABLE


def test_market_closed_phase_never_emits_live_entries():
    engine = make_engine()
    when = ist(18, 0, day=3)  # Saturday
    obs = nse_filing("LT", "Larsen & Toubro Limited", "Bagging of Order", when,
                     summary="Larsen & Toubro bags order worth Rs 3,000 crore")
    engine.ingest([obs], when)
    engine.evaluate(snapshot(when, [Bars("LT", 3500)], status="CLOSED", session="CLOSED"), when + timedelta(minutes=1))
    assert state_of(engine, "LT") in ("WATCH", "NO_SIGNAL")


def test_signal_state_is_stateful_and_alerts_are_not_repeated_every_minute():
    engine = make_engine()
    _order_win(engine)
    lt = Bars("LT", 3500).move(60, 160, 0.0010, volume_mult=3.0)
    bars = [lt] + background_universe(engine, exclude={"LT"})
    alerts = []
    for result in _run(engine, bars, range(61, 100)):
        alerts.extend(item for item in result.alerts if item.symbol == "LT")
    states = [item.to_state for item in alerts]
    assert len(states) == len(set(states)), states  # each actionable state alerted once
    assert len(alerts) <= 3
    transitions = engine.store.transitions()
    assert transitions and all(item["to_state"] for item in transitions)


def test_near_close_blocks_new_entries():
    engine = make_engine()
    when = ist(15, 5)
    minute = int((when - at(0)).total_seconds() // 60)
    obs = nse_filing("LT", "Larsen & Toubro Limited", "Bagging of Order", when,
                     summary="Larsen & Toubro bags order worth Rs 3,000 crore")
    engine.ingest([obs], when + timedelta(seconds=20))
    lt = Bars("LT", 3500).move(minute, minute + 20, 0.0015, volume_mult=4.0)
    _run(engine, [lt] + background_universe(engine, exclude={"LT"}), [minute + 3, minute + 6, minute + 10])
    assert state_of(engine, "LT") not in ACTIONABLE


def test_one_event_affects_multiple_companies_but_indirect_needs_own_confirmation():
    engine = make_engine()
    when = at(60)
    obs = media("Brent crude surges 6% after supply disruption in Middle East", when, publisher="Reuters via ET")
    event = engine.ingest([obs, media("Oil prices jump; Brent crude surges past $90", when, publisher="Mint")],
                          when + timedelta(seconds=60))[-1]
    symbols = {item.symbol for item in event.exposures}
    assert {"ONGC", "BPCL", "INDIGO"} <= symbols
    directions = {item.symbol: item.expected_direction for item in event.exposures}
    assert directions["ONGC"] == "positive" and directions["BPCL"] == "negative"
    # ONGC reacts strongly, BPCL does not move at all.
    bars = [Bars("ONGC", 250).move(60, 120, 0.0015, volume_mult=3.5), Bars("BPCL", 300)]
    bars += background_universe(engine, exclude={"ONGC", "BPCL"})
    _run(engine, bars, [62, 64, 66, 70, 75])
    assert state_of(engine, "BPCL") not in ACTIONABLE
    ongc = [record for record in engine.book.records.values() if record.symbol == "ONGC"]
    assert ongc and all(record.payload["mechanism"]["relationship"] == "COMMODITY" for record in ongc)


def test_multiple_events_for_one_company_aggregate_into_one_ranked_entry():
    engine = make_engine()
    _order_win(engine)
    when = at(62)
    second = nse_filing("LT", "Larsen & Toubro Limited", "Credit Rating Upgrade", when,
                        summary="CRISIL upgraded the credit rating of Larsen & Toubro")
    engine.ingest([second], when + timedelta(seconds=20))
    lt = Bars("LT", 3500).move(60, 120, 0.0012, volume_mult=3.0)
    _run(engine, [lt] + background_universe(engine, exclude={"LT"}), [64, 66, 70, 75])
    symbols = [entry.symbol for entry in engine.top]
    assert symbols.count("LT") <= 1


def test_ranking_is_stable_when_scores_are_close():
    from psygridevents.signal_book import TopRanker
    from psygridevents.storage import StoredSignal

    now = at(60)

    def signal(symbol, score):
        return StoredSignal(f"ev:{symbol}:positive", symbol, f"ev-{symbol}", None, "positive", "CONFIRMED", score,
                            now, now, now, None, None, True, "2026-10-05", {"risk_flags": []})

    ranker = TopRanker(top_n=2, min_score=60, swap_margin=4, multiple_event_bonus=3)
    first = ranker.rank([signal("AAA", 80), signal("BBB", 78), signal("CCC", 70)])
    assert [entry.symbol for entry in first] == ["AAA", "BBB"]
    second = ranker.rank([signal("AAA", 80), signal("BBB", 78), signal("CCC", 80.5)])
    assert [entry.symbol for entry in second] == ["AAA", "BBB"]  # challenger not clearly better
    third = ranker.rank([signal("AAA", 80), signal("BBB", 78), signal("CCC", 90)])
    assert "CCC" in [entry.symbol for entry in third]
    assert all(entry.score >= 60 for entry in third)


def test_threshold_keeps_low_quality_signals_out_of_top():
    engine = make_engine(min_opportunity_score=99.5)
    _order_win(engine)
    lt = Bars("LT", 3500).move(60, 120, 0.0012, volume_mult=3.0)
    _run(engine, [lt] + background_universe(engine, exclude={"LT"}), [62, 66, 70, 80])
    assert not engine.top
    assert state_of(engine, "LT") == "WATCH"
