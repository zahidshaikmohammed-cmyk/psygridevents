# PSYGRIDEVENTS — News Provider Research

## Research objective

The engine is not a headline reader. It must discover, verify, deduplicate, cluster, contextualize and rank events that can materially affect the configured 450-instrument NSE universe.

The provider architecture therefore uses **source diversity with explicit roles** rather than choosing one vendor.

## Findings

### 1. Primary exchange / regulator / government sources — mandatory foundation

**NSE India** is the primary source for listed-company disclosures and exchange information. NSE publishes RSS feeds covering announcements, board meetings, corporate actions, financial results, insider trading, shareholding, related-party transactions, voting results and other corporate-information classes. The exchange states that these feeds provide the latest posted information and are intended to update promptly. The engine should treat NSE disclosures as first-party evidence, not as ordinary media articles.

**BSE** should also be covered as a primary exchange source. BSE's official data platform advertises corporate data APIs covering financials, disclosures, corporate announcements and corporate actions. We should prefer a licensed/official BSE feed or API rather than reverse-engineered endpoints.

**SEBI** provides an official RSS feed containing press releases, circulars and orders/rulings. These are first-party regulatory evidence.

**RBI** provides official RSS feeds for press releases, notifications, speeches and publications. These are first-party central-bank evidence.

**PIB / Government of India** provides official RSS feeds for government press releases. For policy-sensitive events, the engine should prefer the original ministry/department/PIB release over secondary reporting.

### 2. Institutional financial-news feeds — premium target layer

**LSEG / Reuters** is the strongest premium candidate for the system's professional real-time news layer. LSEG states that its Financial News Service combines exclusive Reuters news with thousands of additional sources and delivers news through feeds/APIs. Its programmatic news service is explicitly designed for event-based trading, algorithmic use, risk surveillance and machine consumption, with metadata, topic codes, relevance/confidence, sentiment, significance and deduplication available in relevant offerings.

**Bloomberg Event-Driven Feeds** are another top-tier premium candidate. Bloomberg describes these feeds as structured, machine-readable real-time data for black-box applications and includes breaking headlines, structured financial data, news analytics and economic indicators. Bloomberg's news analytics include sentiment, novelty, readership heat and social velocity.

**FactSet** is a strong institutional alternative. Its developer platform exposes a FactSet News API, Events and Transcripts API, Global Filings API and NLP APIs. This makes it particularly relevant for event extraction, corporate events and document intelligence.

**RavenPack** is especially relevant to the intelligence layer rather than simple news retrieval. Its News Analytics product advertises 40,000+ sources, entity recognition, a taxonomy of 7,000+ event topics, relevance, novelty and impact analytics, and historical plus real-time data. It can therefore serve as an enrichment/benchmarking layer if budget permits.

### 3. Commercial developer-friendly feeds — practical integration layer

**Benzinga** provides real-time financial news APIs, newswire/article APIs, corporate actions, conference-call transcripts, events calendars and a "Why Is It Moving" product. Its API supports filtering by tickers, ISINs/CUSIPs, channels, topics, dates and importance. It is a useful practical provider, but its strongest native coverage is US/North American markets, so it should not be the sole source for an India-first engine.

**Perigon** provides structured global news and events, article search, story clustering, entity lookup and event clustering. Its API is useful as a broad discovery and clustering layer.

**NewsCatcher** provides structured articles, named entities, sentiment, topics, translations, embeddings, deduplication and event clustering. Its stated article latency is roughly 5–10 minutes, with priority-source low-latency options. It is useful for broad discovery and multilingual coverage, but should not replace first-party Indian disclosures.

**GDELT** is valuable as a broad, low-cost global discovery and event-context layer. GDELT 2.0's Event Database and Global Knowledge Graph update every 15 minutes and cover multilingual global news. It is excellent for discovering geopolitical/macro narratives and corroborating broad events, but should not be treated as the authoritative source for a company filing.

**Polygon/Massive Stocks News** exposes ticker-filtered stock news with source metadata, summaries and sentiment/insight fields. It is useful for supplemental global equity news, but the universe and source orientation make it a secondary layer for this India-focused project.

**MarketAux** provides financial news with entity filtering and sentiment endpoints and is useful for inexpensive experimentation and supplemental discovery. It should remain below primary/regulated sources in the evidence hierarchy.

**NewsAPI** is useful for generic article discovery across a large source universe, but its article content can be truncated and it lacks the specialist event/entity semantics required by PSYGRIDEVENTS. It is therefore a discovery fallback, not a core financial intelligence feed.

## Provider decision

PSYGRIDEVENTS should use a **multi-layer provider stack**:

```text
TIER 0 — FIRST-PARTY TRUTH
NSE / BSE / SEBI / RBI / PIB / Ministries / Company IR

TIER 1 — PREMIUM FINANCIAL WIRES
LSEG/Reuters / Bloomberg / FactSet / Dow Jones

TIER 2 — EVENT & NEWS ANALYTICS
RavenPack / Perigon / NewsCatcher / Benzinga

TIER 3 — BROAD DISCOVERY / CONTEXT
GDELT / Polygon-Massive / MarketAux / NewsAPI

TIER 4 — OPEN WEB / SOCIAL DISCOVERY
Used only to generate leads; never treated as confirmation by itself.
```

The numeric tier is an evidence-policy class, **not a claim that every article from a higher tier is true**. Primary evidence can still contain corrections, and secondary sources can reveal information before a filing appears. The engine must retain provenance and allow later corrections.

## Recommended build sequence

1. Activate official NSE/SEBI/RBI/PIB ingestion first.
2. Add a licensed BSE corporate-data adapter when credentials are available.
3. Add company-IR discovery and document ingestion.
4. Add one premium wire provider (LSEG/Reuters or Bloomberg) if/when licensed.
5. Add RavenPack or equivalent event analytics if budget and licensing permit.
6. Add Perigon/NewsCatcher/GDELT for broad discovery and multilingual context.
7. Never collapse all providers into one undifferentiated feed.
8. Store every source observation independently so the engine can measure corroboration, contradiction, novelty and source latency.

## Important licensing rule

The engine must store **metadata, provenance, normalized facts and derived intelligence**. It must not assume that a provider's article body can legally be redistributed. Premium providers must be integrated according to their license/entitlement terms. LSEG explicitly distinguishes programmatic internal use from redistribution; similar checks are required for every commercial provider.

## Source-selection principle

The best provider for PSYGRIDEVENTS is therefore not one company. The best architecture is:

**first-party Indian disclosures + premium financial wire + independent event/news analytics + broad global discovery + strict provenance/verification.**
