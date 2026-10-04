# Source matrix

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
| `nse_announcements` | NSE Corporate Announcements | rss | PRIMARY | corporate_disclosure | 45 | official_catalogue | https://nsearchives.nseindia.com/content/RSS/Online_announcements.xml |
| `nse_board_meetings` | NSE Board Meetings | rss | PRIMARY | corporate_disclosure | 120 | documented_indexed | https://nsearchives.nseindia.com/content/RSS/Board_Meetings.xml |
| `nse_corporate_actions` | NSE Corporate Actions | rss | PRIMARY | corporate_action | 300 | documented_indexed | https://nsearchives.nseindia.com/content/RSS/Corporate_action.xml |
| `nse_financial_results` | NSE Financial Results | rss | PRIMARY | results | 60 | official_catalogue | https://nsearchives.nseindia.com/content/RSS/Financial_Results.xml |
| `nse_insider_trading` | NSE Insider Trading (PIT) | rss | PRIMARY | insider_trading | 300 | official_catalogue | https://nsearchives.nseindia.com/content/RSS/Insider_Trading.xml |
| `nse_circulars` | NSE Exchange Circulars | rss | PRIMARY | exchange_circular | 300 | official_catalogue | https://nsearchives.nseindia.com/content/RSS/Circulars.xml |
| `nse_bulk_deals` | NSE Bulk Deals (archives CSV) | nse_deals_csv | PRIMARY | bulk_block_deal | 900 | documented_indexed | https://archives.nseindia.com/content/equities/bulk.csv |
| `nse_block_deals` | NSE Block Deals (archives CSV) | nse_deals_csv | PRIMARY | bulk_block_deal | 600 | documented_indexed | https://archives.nseindia.com/content/equities/block.csv |
| `nse_surveillance_asm_gsm` | NSE ASM/GSM surveillance lists | unavailable | PRIMARY | surveillance | - | none | ASM/GSM lists are served only through NSE's browser-session JSON API; no documented public feed. Surveillance changes are also announced as NSE circulars (covered by nse_circulars). |
| `nse_fo_ban` | NSE F&O securities in ban period | unavailable | PRIMARY | fo_notice | - | none | No documented feed URL verified; F&O ban/market-wide position limit notices are published as NSE circulars (covered by nse_circulars). |
| `bse_corporate_announcements` | BSE Corporate Announcements | rss | PRIMARY | corporate_disclosure | 60 | official_catalogue | from catalogue https://www.bseindia.com/rss-feed.html |
| `bse_notices` | BSE Notices | rss | PRIMARY | exchange_circular | 300 | documented_indexed | https://www.bseindia.com/data/xml/notices.xml |
| `sebi_rss` | SEBI (press releases, circulars, orders) | rss | OFFICIAL | regulatory | 180 | documented_indexed | https://www.sebi.gov.in/sebirss.xml |
| `rbi_press_releases` | RBI Press Releases | rss | OFFICIAL | central_bank | 120 | documented_indexed | https://rbi.org.in/pressreleases_rss.xml |
| `rbi_notifications` | RBI Notifications | rss | OFFICIAL | central_bank | 300 | documented_indexed | https://rbi.org.in/notifications_rss.xml |
| `rbi_speeches` | RBI Speeches | rss | OFFICIAL | central_bank | 900 | documented_indexed | https://rbi.org.in/speeches_rss.xml |
| `pib_releases` | PIB (all ministries, English) | rss | OFFICIAL | government_policy | 180 | documented_indexed | https://pib.gov.in/RssMain.aspx?ModId=6&Lang=1&Regid=1 |
| `dgft_notifications` | DGFT Notifications (listing page) | html_links | OFFICIAL | trade_policy | 900 | documented_indexed | https://www.dgft.gov.in/CP/?opt=notification |
| `mospi_press_releases` | MoSPI Press Releases (listing page) | html_links | OFFICIAL | macro | 900 | documented_indexed | http://mospi.nic.in/press-release |
| `cci_notifications` | CCI Notifications (listing page) | html_links | OFFICIAL | competition | 1800 | documented_indexed | https://www.cci.gov.in/legal-framwork/notifications |
| `dgtr_findings` | DGTR (anti-dumping / safeguard findings) | unavailable | OFFICIAL | trade_remedy | - | none | No documented feed verified. Customs duty notifications that implement DGTR findings are released via PIB/CBIC; discovery via Google News query 'anti-dumping duty India'. |
| `cbic_gst_council` | CBIC / GST Council | unavailable | OFFICIAL | tax_policy | - | none | No documented public feed verified. GST Council outcomes are released via PIB (Ministry of Finance); discovery via Google News. |
| `egazette` | e-Gazette of India | unavailable | OFFICIAL | gazette | - | none | egazette.gov.in exposes search forms only (no documented feed); not scraped. |
| `ppac` | PPAC (petroleum prices / data) | unavailable | OFFICIAL | commodity | - | none | No documented feed verified; fuel-price/windfall-tax decisions are released via PIB (Petroleum ministry). |
| `trai` | TRAI | unavailable | OFFICIAL | regulatory | - | none | No documented feed verified; tariff/regulation releases appear via PIB and Google News discovery. |
| `cerc` | CERC | unavailable | OFFICIAL | regulatory | - | none | No documented feed verified. |
| `imd` | IMD (monsoon / weather) | unavailable | OFFICIAL | weather | - | none | No documented feed verified; monsoon forecasts are released via PIB (Ministry of Earth Sciences). |
| `nclt` | NCLT | unavailable | OFFICIAL | legal | - | none | Cause lists/orders are behind search forms; not scraped. Listed companies must disclose NCLT outcomes on NSE/BSE (covered). |
| `supreme_court` | Supreme Court of India | unavailable | OFFICIAL | legal | - | none | Judgments are behind CAPTCHA-protected search; never bypassed. Discovery via Google News. |
| `moneycontrol_latest` | Moneycontrol Latest News | rss | REPUTABLE_SECONDARY | financial_media | 120 | curated_directory | http://www.moneycontrol.com/rss/latestnews.xml |
| `economictimes_default` | Economic Times | rss | REPUTABLE_SECONDARY | financial_media | 120 | curated_directory | https://economictimes.indiatimes.com/rssfeedsdefault.cms |
| `business_standard_top` | Business Standard Top Stories | rss | REPUTABLE_SECONDARY | financial_media | 180 | curated_directory | https://www.business-standard.com/rss/home_page_top_stories.rss |
| `livemint_markets` | Mint Markets | rss | REPUTABLE_SECONDARY | financial_media | 180 | curated_directory | https://www.livemint.com/rss/markets |
| `financial_express` | Financial Express | rss | REPUTABLE_SECONDARY | financial_media | 180 | curated_directory | https://www.financialexpress.com/feed/ |
| `hindu_businessline` | The Hindu BusinessLine | rss | REPUTABLE_SECONDARY | financial_media | 300 | curated_directory | https://www.thehindubusinessline.com/feeder/default.rss |
| `google_news_india_markets` | Google News (India markets discovery) | google_news | DISCOVERY_ONLY | discovery | 300 | documented_indexed | https://news.google.com/rss/search (10 queries) |
| `google_news_global_macro` | Google News (global macro discovery) | google_news | DISCOVERY_ONLY | discovery | 600 | documented_indexed | https://news.google.com/rss/search (4 queries) |
| `gdelt_india_business` | GDELT DOC 2.0 (India business discovery) | gdelt | DISCOVERY_ONLY | discovery | 900 | documented_indexed | https://api.gdeltproject.org/api/v2/doc/doc (2 queries) |
