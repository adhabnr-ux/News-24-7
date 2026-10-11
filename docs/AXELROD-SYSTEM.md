# The Axelrod system: first across every big move

Bobby Axelrod doesn't win because he reads faster. He wins because:
- he listens where the information starts;
- he watches what the money does before anyone explains it;
- he finds out *why* the moment something moves;
- he never assumes his edge: after every trade he asks who knew first and why it wasn't him.

News247 and Foretape are built in that order. This page is the map: every kind of big move,
where it first becomes public, what reads it, how fast, and what is still out of reach.

## The five layers

| Layer | What it does | Where it lives |
|---|---|---|
| **1. Hear it at the source** | Reads the first public trace of the news: filings, regulators, company newsrooms, short sellers, scoop feeds, squawks. | `sources/`, [WHERE-THE-NEWS-COMES-FROM.md](WHERE-THE-NEWS-COMES-FROM.md) |
| **2. See the money move** | The price radar (every listed company, 04:00–20:00 ET), the options tape (contracts up thousands of percent, unusual volume, chain-wide surges) and insiders' own buys. | `market/radar.py`, `market/options.py`, `sources/sec_insider.py` |
| **3. Explain any move at once** | A move with no headline sets off an instant lookup of the company's EDGAR filings and a news search. The best explanation is attached to the alert as **Why**. | `market/why.py` |
| **4. Know what's scheduled** | FOMC, CPI, jobs, PDUFA dates, trial readouts, index rebalances, option expiries. Binary events are captured from headlines weeks ahead. | `edge.py` (Calendar), `analysis/catalyst_dates.py` |
| **5. Audit every move** | After each big move: did we have it before the price? If not, was the headline read but scored too low, or never read? The answer goes in the morning Brief. | `scorecard.py` |

## The map: every kind of big move

| Big move | Where it is public first | What reads it | How fast |
|---|---|---|---|
| Company news: deals, guidance, results, FDA | The 8-K on EDGAR, the company's wire release | SEC 8-K feed; PR Newswire, GlobeNewswire, Business Wire and their topic feeds; Alpaca/Benzinga | 10 s (8-K), 20–45 s (wires) |
| "News pending" halts | Nasdaq's halt list | `nasdaq-halts` | 15 s |
| Activist stakes, tender offers, merger filings | EDGAR 13D/13G, SC TO-T, SC 14D-9, 425 | `sec-8k` (paired forms) | 10 s |
| Insiders buying | EDGAR Form 4 (open-market purchases) | `sec-insider-buys` | 30 s |
| Big funds' new stakes | 13F filings (NVIDIA, Alphabet, Amazon, Berkshire) | `sec-13f-giants` | 10 min |
| Short-seller reports | The short seller's own site | 15 `short-*` page watches, `gnews-short-reports` | 2 min |
| Policy: tariffs, sanctions, export rules | Federal Register public inspection, OFAC, BIS, USTR, White House, Truth Social | Those pages and feeds | 5 s bursts at release times, 15–30 s otherwise |
| Sector regulators | FCC, CMS, DOE, NRC, FDA, FTC, DOJ | Page watches and feeds | 1–2 min |
| Courts | Supreme Court slip opinions, Federal Circuit, PACER dockets | Page watches | 2 s from 10:00 ET (SCOTUS) |
| Macro data and the Fed | BLS, the Fed, ECB, BoJ, BoE | Their feeds, polled every second at 08:30, 10:00 and 14:00 ET | 1–15 s |
| AI labs and big tech | Their own newsrooms and blogs | OpenAI, Anthropic, xAI, DeepMind, Google, NVIDIA, Apple, Microsoft and Meta newsrooms; founders' blogs | 10–60 s |
| Index changes | S&P Dow Jones Indices press, Nasdaq | `spdji-press`, `gnews-index-changes` | 1–2 min |
| Newspaper scoops | FT, Bloomberg, WSJ, The Information, Axios, Reuters | Headline feeds, `gnews-scoops` | 1–5 min after the article |
| Geopolitics | CENTCOM, Pentagon contracts, Yonhap, Nikkei, SCMP | Feeds and page watches | 30 s – 2 min |
| Squawk headlines | FinancialJuice, DeItaone | Telegram relays; the X stream (paid) | 5 s; 2–5 s |
| Breaking posts | X, Bluesky | X filtered stream (paid, optional); Bluesky firehose | 1–5 s |
| Odds shifts | Polymarket | `polymarket` | 1 min |
| **No headline at all** | The tape | Radar (every company, pre-market and after hours), options tape, then the **Why** lookup | 1 min (radar); seconds for the lookup |

## The daily loop

1. **08:15 ET, the Brief** (on your phone). It has three parts:
   - Overnight catalysts, ranked.
   - What's on the calendar today, plus tomorrow's binary events.
   - **Last session's scorecard**: for example "9 big moves · ahead on 4 (pushed 3, median 38 min early) · radar first on 3 · 2 scorer gaps".
2. **During the session**, alerts arrive in four kinds:
   - News, pushed at HIGH and CRITICAL.
   - Radar moves, sized to the company.
   - Options spikes, unusual volume and chain-wide surges.
   - **Why** lines, which appear on any move that had no headline, seconds after the alert.
3. **20:00 ET:** the day's scorecard is saved for the long-run record. `news247 scorecard` (or
   `GET /api/scorecard?date=YYYY-MM-DD`) prints every big move with its verdict and what to fix.
4. **Closing a gap:** each verdict says what to do next.

   | Verdict | Meaning | What to do |
   |---|---|---|
   | `scored_low` | The source had it, the rule didn't. | `news247 score '<headline>'` shows why; add the theme to `knowledge.yaml`, then a regression test and a backtest line. |
   | `app` | It was in the app but not pushed. | Same as `scored_low`, at a smaller scale. |
   | `radar` | Price first, then a headline from a known source. | The verdict names the source and the lag. If it rewrote a filing or a release, read the original. |
   | `unexplained` | No headline either side. | It moved on an X post, a broker note or a filing type nobody reads yet. Add the source. |

This is how the CCI miss of Oct 9 2026 was closed ([research/cci-2026-10-09.md](research/cci-2026-10-09.md)).
The same loop is now automatic.

## What "first" means here, honestly

- **Ahead of the price** is the goal: the scorecard counts how often an alert reached you before
  the move. A move with no public trace before it (a whispered deal, an unscheduled filing) can
  only be caught *at* the move; the radar does that, and the Why lookup explains it in seconds.
- **Ahead of the mainstream** is measured story by story ("Your edge" on each alert, and the
  Brief's median head start).
- **Proof on history:** `news247 backtest` replays 190 real market-moving events from 2016 to
  Oct 2026. 96% are caught from the first report, with 0 false alarms on 133 "sounds big but
  isn't" headlines.

## Still out of reach (and what it would cost)

| Gap | Why | Fix |
|---|---|---|
| Truth Social "milliseconds early" | Sold only through the paid Truth API (~$60–100K a month) | Not worth it for alerts; the mirror and squawks follow within seconds |
| Wire terminals' scoops | Bloomberg/Reuters/Dow Jones don't sell to individuals (~$30K a year) | Squawk relays and the X stream follow in seconds |
| Musk, SpaceX and other executives' X posts | Real-time X needs the paid API | Turn on `x-stream` (about $10–60 a month) |
| Options in real time | Free chains are ~15 minutes delayed | A paid OPRA feed (Polygon, Tradier, ThetaData) plugs into `chain_url` |
| A few government and short-seller pages | Unreachable from the build environment, so their link patterns are tested but unverified | Each shows "no links match" in `news247 check` if its markup differs |

## Commands

```bash
news247 scorecard                    # today's big moves: who knew first
news247 scorecard --date 2026-10-09  # any day still in the database
news247 backtest                     # the 190-event replay
news247 score 'headline'             # why a headline scores what it does
news247 check                        # every source once: up, empty or failing
```
