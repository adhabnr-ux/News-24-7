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

## What's in the box

| Lane | Sources | Cost | Typical delay |
|---|---|---|---|
| **AI labs & big tech, first-party** | OpenAI news RSS (10 s), Anthropic newsroom (10 s), OpenAI YouTube, DeepMind, Google, NVIDIA, Apple, Microsoft, Meta newsrooms | free | seconds to the site's own feed lag |
| **Regulators & exchanges** | SEC 8-K filings (10 s), Nasdaq trading halts (15 s), Fed press/monetary feeds, BLS, Treasury, White House, FDA, FTC, DOJ, ECB. **1 s polling at 08:30, 10:00 and 14:00 ET** release times | free | seconds |
| **Scoop publishers** | FT technology/companies/markets, Bloomberg markets/technology, The Information, Axios, WSJ markets, CNBC, MarketWatch bulletins | free (headline feeds) | 1–5 min after the article |
| **Squawk relays** | Telegram (FinancialJuice, every 5 s) | free | seconds |
| **Real-time push** (optional) | **X filtered stream**: OpenAI, sama, Anthropic, DeepMind, xAI, Nvidia, Nadella (VIP) + DeItaone, FirstSquawk, financialjuice, LiveSquawk, unusual_whales, zerohedge, Reuters, Bloomberg, WSJ, CNBC, FT, The Information, Musk, White House | ~$0.005/post (≈ $10–60/month) | 2–5 s |
| | **Alpaca news** (Benzinga newsdesk with tickers) | free (paper-trading keys) | seconds–2 min |
| | **Bluesky** newsrooms (Reuters, AP, NYT, WSJ…) | free | ~1–2 s after they post |
| **Prices** | Yahoo (15 s) or Finnhub websocket (real time) | free | 1–30 s |

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
