# Where News247 gets its news (Bloomberg, or social media?)

**Both, in a specific way.** Market news travels down a relay chain. News247 taps the two ends an individual can reach legally and cheaply:

```
PRIMARY SOURCE  →  WIRES/TERMINALS  →  RETAIL SQUAWKS  →  X / TELEGRAM RELAYS  →  websites, RSS, Google News
company blog,      Bloomberg, Reuters,   Benzinga, Financial-    @DeItaone, @FirstSquawk,    (minutes to hours)
8-K, Fed, BLS,     Dow Jones, AP          Juice, The Fly          @financialjuice
Truth Social,      (~$25–30k/yr each,                             (seconds)
exchange halts      no individual API)
     ▲                                                                         ▲
     └── News247 watches these directly (free, often FIRST) ── and these relays (seconds after the wires)
NEWSPAPER SCOOPS (FT, WSJ, Bloomberg News, The Information) enter at the wire level within seconds-minutes.
```

## The "Axelrod lane": where 2026's movers appeared first

In *Billions*, Axelrod wins by hearing things first. We traced ~58 market-moving stories from June to October 2026 back to their first public appearance ([research/first-sources-2026.md](research/first-sources-2026.md)). After company releases and 8-Ks, the first places were:
- government documents before announcements (Federal Register public inspection, OFAC, BIS/USTR, CENTCOM, FAA);
- court opinion pages;
- officials' own X posts (FHFA's Pulte crashed FICO twice);
- EDGAR filings a day ahead of the press release (Generac, Nvidia's 13G);
- founders' personal sites on weekends (Amodei's essay);
- newspaper scoops with tell-tale wording ("Exclusive-", "Said to", "held talks", "people familiar").

News247 checks those first, every 5–60 s, with bursts at the minutes they usually publish. Polymarket odds jumps are a heads-up only.

**The honest limit:** Trump's posts are now sold "milliseconds" early through the paid Truth API ($60–100K/month), and wire terminals still beat any free feed on scoops by seconds. Free sources can't be first on those. Everything else on the list above is public, free and often hours ahead of the coverage.

## What's in the box

| Lane | Sources | Cost | Typical delay |
|---|---|---|---|
| **AI labs & big tech, first-party** | OpenAI news RSS (10 s), Anthropic newsroom (10 s), OpenAI YouTube, DeepMind, Google, NVIDIA, Apple, Microsoft, Meta newsrooms | free | seconds to the site's own feed lag |
| **First places** (Oct 2026 research) | Federal Register public inspection (5 s bursts at 08:45/11:15/16:15 ET), OFAC Recent Actions, BIS, USTR, Supreme Court slip opinions (2 s from 10:00 ET), Federal Circuit, PACER (D.D.C., E.D. Va.), FHFA, CENTCOM, Pentagon contracts (17:00 ET), FAA, darioamodei.com, blog.samaltman.com, Polymarket odds jumps, EDGAR 13D/13G/SC TO-T/425/6-K | free | seconds to a minute |
| **Ahead of the wires** (Oct 11 2026) | SEC Form 4 insider purchases (10 s), 15 activist short sellers' own sites (2 min), FCC headlines, CMS newsroom, DOE newsroom, NRC news (1–2 min), S&P Dow Jones Indices press (1 min), SC 14D-9 tender responses; Google News for SpaceX/Starlink/xAI deals (90 s) and short reports (2 min) | free | seconds to 2 min |
| **Regulators & exchanges** | SEC 8-K filings (10 s), Nasdaq trading halts (15 s), Fed press/monetary feeds, BLS, Treasury, White House, FDA, FTC, DOJ, ECB. **1 s polling at 08:30, 10:00 and 14:00 ET** release times | free | seconds |
| **Scoop publishers** | FT technology/companies/markets, Bloomberg markets/technology, The Information, Axios, WSJ markets, CNBC, MarketWatch bulletins | free (headline feeds) | 1–5 min after the article |
| **Squawk relays** | Telegram (FinancialJuice, every 5 s) | free | seconds |
| **Real-time push** (optional) | **X filtered stream**: OpenAI, sama, Anthropic, DeepMind, xAI, Nvidia, Nadella (VIP) + DeItaone, FirstSquawk, financialjuice, LiveSquawk, unusual_whales, zerohedge, Reuters, Bloomberg, WSJ, CNBC, FT, The Information, Musk, White House | ~$0.005/post (≈ $10–60/month) | 2–5 s |
| | **Alpaca news** (Benzinga newsdesk with tickers) | free (paper-trading keys) | seconds–2 min |
| | **Bluesky** newsrooms (Reuters, AP, NYT, WSJ…) | free | ~1–2 s after they post |
| **Prices** | Yahoo (15 s) or Finnhub websocket (real time) | free | 1–30 s |

## Ahead of the wires: who knows first, and where they post it

*Billions* rule: the edge is knowing what the people closest to the money are doing, before it is
a headline. Six places publish that first, for free:

| Source | What it catches | How fast | Example |
|---|---|---|---|
| **SEC Form 4, open-market buys** (`sec-insider-buys`) | A CEO, CFO, director or 10% owner buying their own stock with their own cash | EDGAR lists the filing the minute it is accepted (it must be filed within 2 business days of the trade); polled every 10 s, several filings read at once; routine same-month traders, 10b5-1 plans and offerings are told apart | "CEO Jane A Smith buys $2.5M of Acme Biotech (ACMB) stock at $3.10-$3.30" |
| **Activist short sellers' own sites** (`short-*`, 15 firms) | A new short report: Muddy Waters, Spruce Point, Culper, Blue Orca, Kerrisdale, Fuzzy Panda, Wolfpack, Grizzly, Morpheus, Iceberg, Viceroy, J Capital, Bleecker Street, Scorpion, Citron | Every 2 min. The report is on their site before the wires and often before the open | Hindenburg on Icahn Enterprises, May 2 2023: IEP −20% that day |
| **FCC headlines** (`fcc-headlines`) | Spectrum sales, satellite-to-phone approvals, auctions | Every minute | SpaceX/EchoStar, SpaceX/Grain 800 MHz: carriers −10–13%, towers +7–16% |
| **CMS newsroom** (`cms-newsroom`) | Medicare Advantage rate notices and star ratings | Every minute; the April rate notice lands after the close | Apr 7 2025: +5.06% vs +2.23% proposed; HUM, UNH, CVS sharply higher the next day |
| **DOE newsroom, NRC news** (`doe-newsroom`, `nrc-news`) | Nuclear selections, fuel awards, loans, construction permits and licenses | Every 2 min | OKLO, SMR, NNE and LEU move 10–30% on these |
| **S&P Dow Jones Indices press** (`spdji-press`) | S&P 500 additions and replacements ("Set to Join", "Replace", "Rebalance") | Every minute; quarterly changes come after the close (~17:15 ET) on the first Friday of March, June, September and December, replacements any day | Added companies usually gap up the next morning (HOOD and APP, Sep 2025) |

**How insider buys are scored.** Sales are ignored (executives sell for taxes, houses and
diversification); so are grants, option exercises, gifts and tax withholding. Only purchases
(transaction code P) worth $250K or more count. Each one is weighed by:

- **Who:** the CEO, CFO, President or Chair adds more than a director.
- **How much:** $1M or more adds more.
- **Against the company's size:** 1% or more of the market value counts most, then 0.2% and 0.05%.
- **Clusters:** a second insider buying the same stock within 7 days adds more.

A CEO putting 1.7% of a $150M company into its stock is HIGH and pushes. A director's $550K buy in
a $30B company is shown in the app but not pushed.
The small-cap lane labels it "Insider buying", never a takeover.

**How short reports are filtered.** Each short seller's page is watched only for report-shaped
links: multi-word slugs or PDFs. Navigation pages (about, contact, team, category, privacy,
terms…) are excluded. The firm's name plus a report word ("short", "report", "alleges",
"fraud", "targets"…) is what triggers the `short_seller_report` rule, so "the Hindenburg
disaster" and "muddy waters for the Fed" don't.

**Status.** These pages could not be fetched from the build environment (government sites
are blocked there). If a site's markup differs from the pattern, that source shows "no links
match link_pattern" on the dashboard and in `news247 check`. It is never silently empty.
Hindenburg Research closed in January 2025, so it is recognized in news but not watched.

**Still not free:** SpaceX, Elon Musk and Truth Social posts reach you in real time only through
the paid X stream above. The free path for SpaceX deals is Google News every 90 s.

## Which events each budget would have caught in time

| Event | $0 (built in) | + X stream (~$10–60/mo) |
|---|---|---|
| FT scoop on OpenAI revenue (Oct 8, 2026) | Yes: FT/Bloomberg headline feeds in ~1–5 min, while the selloff ran ~30 min | Yes, in seconds via squawk relays |
| Tariff pause post (Apr 9, 2025) | Late, ~1–10 min via relays (the rally ran for hours) | Seconds |
| CPI / jobs report | Yes: BLS polled every second at 08:30 ET, plus bulletins | Same, plus squawks |
| AI-lab product launch (Cowork plugins, DeepSeek-R1) | **Yes, and days early.** The selloffs came 2–7 days after the posts | Same |
| SEC 8-K shocks (auditor quits, guidance suspended) | Yes, ~10–30 s | Same |

## Why not Bloomberg/Reuters directly?
- A Bloomberg Terminal costs ~$28–32k a year.
- Reuters, Dow Jones and AP sell feeds only to enterprises, and none offers an individual API.
- What they add is a few seconds on newspaper scoops and machine-readable economic data in under a second. That matters if you trade in seconds; it's not worth $30k for alerts.
- Truth Social's official "Truth API" (Aug 2026) is reported at $60–100k/month.

## Rules we follow
- **X:** only the official paid API (scraping X is prohibited by its terms).
- **SEC:** a descriptive User-Agent with your e-mail, and at most 10 requests/second (enforced by the HTTP client).
- **Nasdaq halts and publisher feeds:** personal use only. Don't republish alerts publicly.
- **Squawk relays are unvetted:** single-relay alerts are labelled **UNCONFIRMED** until a real source matches.

Details and sources: [research/news-sources.md](research/news-sources.md).
