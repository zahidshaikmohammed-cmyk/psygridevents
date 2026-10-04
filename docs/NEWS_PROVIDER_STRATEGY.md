# News Provider Strategy

## Decision

PSYGRIDEVENTS should not buy multiple overlapping premium feeds before measuring coverage.

For the 990-stock Indian intraday objective:

1. Keep the existing first-party feeds enabled: NSE, SEBI, RBI and PIB.
2. Primary premium target: LSEG Financial News / Reuters Machine Readable News.
3. Optional second layer after measurement: RavenPack News Analytics.
4. Benzinga is an experiment only; its public stock-news coverage is centered on North American equities, although Benzinga documents additional regional India coverage.
5. Do not add a second Dhan market-data subscription. PSYGRID already supplies the 990-stock live 1-minute OHLCV feed consumed by this repository.

## Why LSEG first

LSEG documents real-time Reuters news, third-party newswires/exchanges, machine-readable delivery, event-driven/algorithmic trading use cases, relevance/significance/confidence metadata, and historical archives. LSEG also states that Reuters India financial-file coverage was expanded.

The required commercial entitlement is the programmatic News Feed / Machine Readable News product, not a normal Workspace desktop subscription. Workspace news is intended for individual use and does not provide the server-side entitlement required by this engine.

The exact price is quote-based and must be requested from LSEG for the Indian, internal programmatic use case.

## Why RavenPack second

RavenPack is particularly valuable after raw coverage is solved. Its public documentation describes 40,000+ news/social sources, entity/event tagging, relevance, novelty, temporal scoring and impact analytics. That makes it a strong enhancement to PSYGRID's event materiality and novelty layers.

RavenPack pricing is also quote/trial based, so it should be evaluated after the LSEG coverage benchmark rather than purchased blindly.

## Coverage benchmark

Before declaring the stack complete, record for each provider:

- unique stories received
- duplicate/story-cluster rate
- median and p95 publication-to-observation latency
- 990-stock entity resolution coverage
- number of material events
- number of events preceding >0.5%, >1%, >2% and >3% moves
- 15-minute and 30-minute forward outcomes
- missed-mover count
- false-positive count
- source overlap

Run this for at least 20-30 Indian trading sessions.

## Adapter rule

Provider-specific transport and schemas must stay behind adapters. The canonical RawObservation contract remains the boundary into the existing normalization, deduplication, clustering, entity-resolution, materiality, exposure and signal layers.

No provider adapter may contain trading decisions.

## Current implementation

news_providers.py introduces a provider-neutral contract for licensed feeds and converts normalized provider messages into the existing RawObservation model.

Credentials and provider-specific endpoints remain external configuration. A provider must not be marked enabled until its commercial entitlement and transport have been verified.
