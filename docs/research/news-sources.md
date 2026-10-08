# Research: market-news sources in 2026 (speed, cost, legality)

Compiled 2026-10-08 via web search. Prices are third-party estimates unless the vendor publishes them. Items marked **[unverified]** need checking.

## Terminals and wires
| Source | Price | Individual API? | Free public surfaces |
|---|---|---|---|
| Bloomberg Terminal (+ First Word) | ~$27.7–32k/yr ([Eulerpool](https://eulerpool.com/blog/bloomberg-terminal-cost), [CostBench](https://costbench.com/software/financial-data-terminals/bloomberg-terminal/)) | No (B-PIPE is enterprise, ~$50–200k+) | RSS `bloomberg.com/feeds/markets/news.rss` (articles, minutes behind), X, Bluesky |
| Reuters (LSEG) | ~$2–22k/user/yr | Enterprise only | **No public RSS since 2020**; Bluesky `reuters.com`; Google News `site:reuters.com` |
| Dow Jones Newswires | Enterprise | No | WSJ feeds `feeds.content.dowjones.io/public/rss/...`; MarketWatch bulletins |
| AP | Enterprise | No | Bluesky `apnews.com` |

## Retail squawk and news services
| Service | Price | Machine-readable? |
|---|---|---|
| Benzinga Pro | ~$37–197/mo; squawk add-on $99 | The terminal isn't an API. **Benzinga via Massive is $99/mo**; **Alpaca news stream** (Benzinga) is on Alpaca's data plans; whether the free plan includes it is [unverified], so test it with a free key |
| FinancialJuice | Free tier; Pro ~$69/quarter | No API; free X and Telegram relay (`t.me/s/FinancialJuice`) |
| Trade The News / The Fly / Newsquawk | $35–399/mo | No retail APIs |
| @DeItaone ("Walter Bloomberg"), @FirstSquawk | Free on X | Only via the paid X API. **Unvetted:** on Apr 7, 2025 a misread "90-day pause" spread 10:11 → 10:13 via @DeItaone and swung the S&P by trillions before the White House called it fake ([PolitiFact](https://www.politifact.com/factchecks/2025/apr/08/tweets/trump-hassett-tariff-pause-stock-market-rally-x/)) |
| Unusual Whales | API websocket for personal use on the Advanced plan | Yes (paid) |
| Truth Social "Truth API" | Reported $60–100k/month, institutional, live Aug 2026 ([Fortune](https://fortune.com/2026/08/12/trump-media-truth-api-insider-trading/)) | Third parties (1322.io from ~$250/mo) scrape, and TMTG says that breaches its ToS |

## Primary sources (free, legal, often first)
- **SEC EDGAR:** live 06:00–22:00 ET. Filings after 17:30 are disseminated the next business day. Fair-access limit is 10 requests/second, with a declared User-Agent ([SEC](https://www.sec.gov/os/accessing-edgar-data)).
- **Press-release wires** (Business Wire, PR Newswire, GlobeNewswire): virtually all Reg FD disclosures go out here first.
- **BLS/BEA:** data posts at exactly 08:30 ET. Media lockups ended in 2020 ([BLS](https://www.bls.gov/bls/discontinuing-department-of-labor-media-lockup-effective-june-3.pdf)), so a DIY fetch gets you there within seconds.
- **Fed:** FOMC statement at 14:00 ET. Feeds: `federalreserve.gov/feeds/press_monetary.xml` and `press_all.xml`.
- **Nasdaq trade halts RSS:** personal use only; don't redistribute.
- **Company newsrooms, blogs and IR pages:** AI product launches appear here first.

## Social media's role
- **X** is still the fastest *public* relay.
  - Free API tier closed to new developers Feb 2026; pay-per-use is about $0.005 per post read ([docs.x.com](https://docs.x.com/x-api/getting-started/pricing)).
  - Scraping is prohibited, with liquidated damages in the terms.
- **Bluesky Jetstream:** free, no key, real-time ([jetstream](https://echo.archivarix.net/de/archive/bluesky-firehose-jetstream)). Reuters and AP post there. Publishers report putting *less* effort into Bluesky in 2026.
- **Telegram:** public channel previews (`t.me/s/<channel>`) are readable without an account. Many channels are scams; FinancialJuice is legitimate.
- **Reddit/WSB:** a sentiment signal, not a news-breaking source.

## Newspaper scoop feeds (free headline RSS)
| Outlet | Feed |
|---|---|
| FT | `ft.com/<section>?format=rss` (technology, companies, markets) |
| WSJ | `feeds.content.dowjones.io/public/rss/RSSMarketsMain` |
| Bloomberg | `bloomberg.com/feeds/{markets,technology}/news.rss` |
| The Information | `theinformation.com/feed` [unverified] |
| Axios | `api.axios.com/feed/` |
| Reuters | Google News `site:reuters.com when:1h` |

## Cheap news APIs
| API | Notes |
|---|---|
| Alpaca news WS | free(?) / $99 |
| Finnhub | Free is non-commercial; news WS is premium |
| Massive Benzinga | $99/mo |
| Marketaux | 100 requests/day free |
| NewsAPI | Free tier is delayed 24h |
| GDELT | 15-minute cycle |
| Google News RSS | Minutes to an hour behind |

## Recommendation by budget
- **$0:** primary sources (EDGAR, newsrooms, Fed/BLS at release times), headline RSS from the scoop publishers, Bluesky Jetstream, Telegram FinancialJuice, Nasdaq halts, Alpaca if entitled. **This is the default configuration.**
- **~$20–60/mo:** add the X API for 20–50 high-signal accounts. Require cross-confirmation before treating relay alerts as fact.
- **~$100–300/mo:** add a licensed real-time feed (Benzinga via Massive $99, or Alpaca's paid plan), optionally 1322.io for Truth Social (ToS caution).

The scenarios table in [WHERE-THE-NEWS-COMES-FROM.md](../WHERE-THE-NEWS-COMES-FROM.md) shows which events each tier would have caught in time.
