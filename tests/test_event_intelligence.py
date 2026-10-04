"""Fusion, entity resolution, classification, novelty/contradiction and exposure for canonical events."""
from __future__ import annotations

from datetime import timedelta

import pytest
from live_support import at, ist, make_engine, media, nse_filing

from psygridevents.entity_resolution import EntityResolver
from psygridevents.event_classifier import EventClassifier
from psygridevents.nse_parsing import parse_nse_filing_link
from psygridevents.settings import CONFIG_DIR
from psygridevents.universe import load_instruments


@pytest.fixture(scope="module")
def resolver() -> EntityResolver:
    return EntityResolver.from_files(load_instruments(), CONFIG_DIR / "issuer_aliases.yaml")


# --------------------------------------------------------------------------- entity resolution
@pytest.mark.parametrize("text", [
    "Crude oil India imports rise to record",          # 'Oil India' only as lowercase prose
    "Reserve Bank of India keeps repo rate unchanged",  # 'Bank of India' inside a blocked phrase
    "OIL PRICES SURGE ON SUPPLY FEARS",                 # shouting headline, ambiguous symbol
    "Star performers of the week",                      # STAR is an ambiguous common word
    "The idea was rejected by the board",               # IDEA lowercase
])
def test_weak_name_only_coincidences_do_not_resolve(resolver, text):
    assert resolver.resolve(text) == []


def test_strong_identifiers_resolve_with_basis(resolver):
    matches = {item.symbol: item.basis for item in resolver.resolve("Bank of India Q2 profit jumps; NSE: OIL rallies")}
    assert matches == {"BANKINDIA": "curated_name", "OIL": "exchange_tag"}
    filing = resolver.resolve("Some Company Limited", nse_filing_symbol="DIXON")
    assert filing[0].symbol == "DIXON" and filing[0].basis == "nse_filing_link"
    brand = resolver.resolve("Airtel raises mobile tariffs")
    assert brand[0].symbol == "BHARTIARTL" and brand[0].basis.startswith("alias:")


def test_official_names_and_universe_validation():
    resolver = EntityResolver(["ABC", "XYZW"], official_names={"ABC": "Alpha Beta Chemicals Limited", "NOPE": "Ghost Ltd"},
                              curated={"issuers": {"NOTINUNIVERSE": {"names": ["Phantom Corp"]}}})
    assert resolver.dropped == ["NOTINUNIVERSE"]
    assert [m.symbol for m in resolver.resolve("Alpha Beta Chemicals wins export order")] == ["ABC"]
    assert resolver.resolve("Ghost Ltd and Phantom Corp") == []


def test_nse_filing_link_gives_symbol_and_dissemination_clock():
    link = parse_nse_filing_link("https://nsearchives.nseindia.com/corporate/ADSL_21052026224133_OutcomeofBoardMeeting.pdf")
    assert link is not None and link.symbol == "ADSL"
    assert link.disseminated_at.isoformat() == "2026-05-21T17:11:33+00:00"
    assert "Board Meeting" in link.subject
    assert parse_nse_filing_link("https://example.com/corporate/ADSL_21052026224133_X.pdf") is None


# --------------------------------------------------------------------------- classification
@pytest.mark.parametrize("title,subtype,direction", [
    ("Bagging of order worth Rs 450 crore from NHAI", "order_win", "positive"),
    ("Termination of contract by customer", "order_cancellation", "negative"),
    ("Receipt of warning letter from USFDA for Unit II", "usfda_adverse", "negative"),
    ("Closure of Trading Window", "routine_disclosure", "neutral"),
    ("Newspaper publication of financial results", "routine_disclosure", "neutral"),
    ("Outcome of Board Meeting - approval of buyback", "buyback", "positive"),
    ("Q2 net profit falls 32% on weak margins", "results_negative", "negative"),
    ("RBI cuts repo rate by 25 basis points", "rate_cut", "positive"),
])
def test_classifier(title, subtype, direction):
    primary, _ = EventClassifier().classify(title)
    assert (primary.subtype, primary.direction) == (subtype, direction)


def test_conflicting_result_language_is_mixed_not_forced():
    primary, _ = EventClassifier().classify("Revenue rises 12% but net profit falls 8%")
    assert primary.direction == "mixed"


# --------------------------------------------------------------------------- fusion / duplicates
def test_duplicate_observation_is_ingested_once():
    engine = make_engine()
    obs = nse_filing("LT", "Larsen & Toubro Limited", "Bagging of Order", at(30))
    engine.ingest([obs, obs], at(31))
    engine.ingest([obs], at(40))  # re-polled later
    assert engine.store.counts()["observations"] == 1
    assert engine.counters["observations_duplicate"] == 2


def test_five_sources_one_event_make_one_story_with_evidence():
    engine = make_engine()
    when = at(45)
    reports = [nse_filing("LT", "Larsen & Toubro Limited", "Bagging of Order", when,
                          summary="Larsen & Toubro bags order worth Rs 2,500 crore from NHAI")]
    for publisher in ("Economic Times", "Mint", "Business Standard", "Moneycontrol"):
        reports.append(media(f"Larsen & Toubro bags Rs 2,500 crore NHAI order - {publisher}", when + timedelta(minutes=3),
                             publisher=publisher))
    events = engine.ingest(reports, when + timedelta(minutes=5))
    story_ids = {event.story_id for event in events}
    assert len(story_ids) == 1
    event = events[-1]
    assert event.source_count == 5 and event.publisher_count == 5
    assert event.confirmation_status == "CONFIRMED_PRIMARY"
    assert event.source_quality == "PRIMARY"
    assert event.public_at == when  # first public time comes from the exchange
    assert event.magnitude_crore == 2500


def test_different_events_for_same_company_stay_separate():
    engine = make_engine()
    a = nse_filing("LT", "Larsen & Toubro Limited", "Bagging of Order", at(30),
                   summary="Larsen & Toubro bags order worth Rs 800 crore")
    b = nse_filing("LT", "Larsen & Toubro Limited", "Resignation of Chief Financial Officer", at(35),
                   summary="CFO resigns citing personal reasons")
    events = engine.ingest([a, b], at(36))
    assert len({event.story_id for event in events}) == 2
    assert {event.subtype for event in events} == {"order_win", "key_management_exit"}


def test_conflicting_sources_reduce_confidence_until_primary_resolves():
    engine = make_engine()
    when = at(30)
    rumour = media("Larsen & Toubro wins Rs 5,000 crore metro order", when, publisher="Some Portal")
    denial = media("Larsen & Toubro metro order cancelled, contract terminated", when + timedelta(minutes=10),
                   publisher="Another Portal")
    events = engine.ingest([rumour, denial], when + timedelta(minutes=11))
    event = events[-1]
    assert event.contradictions and event.contradiction_status == "open"
    assert event.confirmation_status == "CONTRADICTED"
    primary = nse_filing("LT", "Larsen & Toubro Limited", "Clarification on news item", when + timedelta(minutes=20),
                         summary="Larsen & Toubro clarifies the metro contract was terminated by the customer")
    resolved = engine.ingest([primary], when + timedelta(minutes=21))[-1]
    assert resolved.story_id == event.story_id
    assert resolved.confirmation_status == "CONFIRMED_PRIMARY"
    assert resolved.contradiction_status == "resolved"
    assert all(item["status"] == "resolved" for item in resolved.contradictions)
    assert resolved.direction == "negative"  # the exchange filing (termination) prevails over the rumour


def test_discovery_only_event_is_flagged_and_capped():
    engine = make_engine()
    event = engine.ingest([media("Larsen & Toubro bags Rs 3,000 crore order", at(30), publisher="news.google",
                                 quality="DISCOVERY_ONLY", provider="google_news_india_markets")], at(31))[0]
    assert event.confirmation_status == "UNCONFIRMED_DISCOVERY"
    assert not event.actionable_quality


def test_roundup_articles_do_not_create_single_stock_events():
    engine = make_engine()
    event = engine.ingest([media("Stocks to watch: Larsen & Toubro, Infosys, Tata Steel, Bharti Airtel, Coal India",
                                 at(-30))], at(-29))[0]
    assert event.roundup and not event.exposures


def test_routine_filing_is_kept_but_never_signalled():
    engine = make_engine()
    event = engine.ingest([nse_filing("LT", "Larsen & Toubro Limited", "Closure of Trading Window", at(10))], at(11))[0]
    assert event.routine and event.materiality_score <= 0.05 and not event.exposures


def test_repeat_of_old_story_is_not_new_information():
    engine = make_engine()
    first = engine.ingest([media("Larsen & Toubro bags Rs 2,500 crore order from NHAI for expressway", at(-60 * 20),
                                 publisher="Economic Times")], at(-60 * 20))[0]
    engine.storybook.expire(at(60 * 60))  # force a separate story by expiring the window
    later = engine.ingest([media("Larsen & Toubro bags Rs 2,500 crore order from NHAI for expressway project", at(60),
                                 publisher="Mint", url="https://example.invalid/mint-repeat")], at(61))[0]
    assert later.story_id != first.story_id
    assert later.novelty_status in ("repeat", "stale_repackaged", "follow_up", "updated")


def test_future_timestamp_is_never_used_as_the_event_clock():
    engine = make_engine()
    obs = media("Larsen & Toubro bags Rs 900 crore order", at(30) + timedelta(hours=5), publisher="Clocky")
    obs = obs.__class__(**{**obs.__dict__, "observed_at": at(30)})
    event = engine.ingest([obs], at(30))[0]
    assert event.public_at == at(30)
    assert event.public_at_basis == "first_seen"


def test_stale_news_cannot_become_a_live_signal():
    engine = make_engine()
    old = ist(10, 0, day=2)
    engine.ingest([nse_filing("LT", "Larsen & Toubro Limited", "Bagging of Order", old,
                              summary="Larsen & Toubro bags order worth Rs 2,500 crore")], old)
    from live_support import Bars, background_universe, snapshot
    lt = Bars("LT", 3500).move(30, 80, 0.0015, volume_mult=3)
    now = at(60)
    engine.evaluate(snapshot(now, [lt] + background_universe(engine, exclude={"LT"})), now)
    assert not engine.top  # old (beyond max_candidate_age) news is not evaluated as a live opportunity


def test_macro_event_maps_to_exposed_groups_with_mechanism_and_direction():
    engine = make_engine()
    event = engine.ingest([media("RBI cuts repo rate by 25 basis points to 5.25%", at(45), publisher="PIB",
                                 quality="OFFICIAL", provider="rbi_press_releases")], at(46))[0]
    assert event.factor == "policy_rate" and event.factor_direction == "down"
    by_symbol = {item.symbol: item for item in event.exposures}
    assert by_symbol["DLF"].relationship == "MACRO" and by_symbol["DLF"].expected_direction == "positive"
    assert all(item.mechanism and 0 < item.confidence <= 1 for item in event.exposures)
    persisted = engine.store.exposures_for_event(event.event_id)
    assert len(persisted) == len(event.exposures)


def test_unrelated_headlines_without_companies_are_not_merged_but_rewrites_are():
    engine = make_engine()
    headlines = [
        "RBI keeps repo rate unchanged, maintains neutral stance",
        "Brent crude surges 5% after OPEC output cut",
        "Rupee falls to record low against the dollar",
        "Government imposes anti-dumping duty on Chinese steel",
        "GST Council recommends rate cut on insurance premiums",
        "Monsoon rainfall 8% above normal, says weather office",
    ]
    events = engine.ingest([media(title, at(10 + i), publisher=f"P{i}", url=f"https://x.invalid/u{i}")
                            for i, title in enumerate(headlines)], at(30))
    assert len({event.story_id for event in events}) == len(headlines)
    rewrite = engine.ingest([media("Brent crude surges 5% after OPEC output cut deal", at(20), publisher="Mint",
                                   url="https://x.invalid/rewrite")], at(31))[0]
    crude = next(event for event in events if "Brent" in event.headline)
    assert rewrite.story_id == crude.story_id
