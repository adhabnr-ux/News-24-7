# The Camillo desk

Chris Camillo's method, social arbitrage, runs as rules over the trend lane
([TRENDS.md](TRENDS.md)). It has its own tab in Foretape, the **Camillo** page, and its own API,
`GET /api/camillo`.

**Sources.** Everything the page says about him is sourced in `news247/data/camillo.yaml`:
interviews, his posts, Jack Schwager's *Unknown Market Wizards* chapter, and his book *Laughing
at Wall Street*. Quotes are as those sources print them. The research couldn't open the book texts
themselves; where a claim rests on a summary of a book, its label says so. Anything only folklore
repeats is listed as unverified and never shown as fact.

## The method in one paragraph

> "I invest when there's an information imbalance, and I exit the investment when we reach
> information parity." (Stansberry Investor Hour, Feb 2021)

The steps:
1. Notice a change in what people do: a product selling out, a habit spreading, conversation
   accelerating.
2. Tie it to the listed company it moves, where it is big enough to matter.
3. Act only while Wall Street hasn't told the story yet.
4. Leave when the market agrees, win or lose, with no price target.

He estimated that 99% of his ideas never become trades, and says he would skip the
low-conviction ones if he could do it again.

## How the desk does each step

| His step | On this desk |
|---|---|
| Detect the change | Five feeds: **Wikipedia attention** (daily), **Bluesky chatter** counted by brand "tags" (hourly sample, the TickerTags idea), **App Store climbs** (6-hourly), **sell-outs/waitlists** and **tracker numbers** in the news |
| Map it to the company | About 50 brands mapped to their owners (`data/trends.yaml`); app developers matched to listed names |
| Validate | Pushed only when two independent kinds of evidence agree on a direction; the direction comes from the numbers |
| It has to matter | Our rule, not his: up to $150B when the brand is the company, $60B for a headline brand, $20B for a name match |
| Confirm the imbalance | The **consensus score** from his book, plus a price check: out if the stock already moved 15% the trend's way |
| The catalyst | The next report date from Nasdaq's earnings calendar |
| Conviction | HIGH ideas pass every filter and are pushed; MEDIUM ideas wait in the app; LOW ideas stay on the board |
| Exit at parity | Each idea shows its exit: consensus score above 2, the report, or the evidence stopping. Every idea is scored against SPY at 30/60/90 days |

### The consensus score

His book's test for information parity, as readers have summarized it:
- List the "hype" (bullish) quotes and the "gloom and doom" (bearish) quotes from journalists and
  analysts, one per source.
- Divide hype by gloom.

| Score | Reading |
|---|---|
| Above 2 | Parity: the story is told, get out |
| 0.5 to 2 | An imbalance |
| Below 0.5 | A strong imbalance |

The desk applies this to every headline it has read in the last 30 days that names the brand
(`camillo.consensus_score`, `camillo.coverage`). Each source counts once per side. For a short
idea the sides swap: gloom is the trend's side. When no headline is telling the story yet,
that counts as a strong imbalance.

### TickerTags chatter

His company TickerTags mapped words to tickers and tracked how often people said them. M Science
(Jefferies) bought it in June 2018.

`camillo.ChatterTags` does the same on Bluesky's public firehose (Jetstream, free):
- **Sampling:** it reads 10 minutes every hour (`chatter_sample_s`) and counts English posts only.
- **Tags:** every brand term in `trends.yaml` is a tag, counted once per post.
- **What it measures:** the brand's **share of all posts sampled**, so a busier network doesn't
  look like a trend.
- **A spike needs all three:**
  - the last 3 days at **3x** the median of the 14 days before (`chatter_ratio`);
  - on at least 2 of those 3 days;
  - with **30+ mentions** (`chatter_min_mentions`).
- **No direction:** chatter carries none, like Wikipedia attention, so it confirms an idea but
  can't push one alone.

### The checklist on every idea

| Test | Pass when |
|---|---|
| People are doing something new | Any evidence |
| Seen in two independent places | 2+ kinds |
| It points one way | Up or down, not attention only |
| Big enough to move this company | Market cap under the brand weight's limit |
| An information imbalance | Consensus score not above 2 |
| The price hasn't caught up | Under 15% the trend's way since the evidence began |
| A report ahead will reveal it | Next report within 120 days |

## The page

1. **Live ideas:**
   - every signal from the last 45 days, strongest first;
   - its evidence and links;
   - the checklist with the numbers behind each test;
   - the exit plan.
2. **Heating up:** brands drawing unusual Wikipedia attention or Bluesky chatter that isn't a signal
   yet.
3. **The method:**
   - his principles, with quotes and sources;
   - the eight-step process, mapped to this desk;
   - what he avoids;
   - his record as reported;
   - what this desk can't do;
   - what's said about him without a source.
4. **Case studies:** 11 of his trades, each with a source:
   - Snapple puts;
   - Nintendo at E3;
   - J.Crew;
   - LeapFrog;
   - Newell and slime;
   - Netflix and *Stranger Things*;
   - Dorel bikes;
   - Celsius at parity;
   - Bloom Energy;
   - Under Armour (the one he regrets);
   - Restaurant Brands (a loss).

   Where we checked the stock's daily bars ourselves, the "price check" line says so. It isn't his
   profit.
5. **Track record:** how this desk's ideas did against the S&P 500.
6. **The feeds:** when each source was last read.

## What it can't do

- **The platforms he reads:** he reads TikTok, Instagram and X, which have no free API. The desk
  reads Bluesky, Wikipedia and the App Store instead, so TikTok-only trends reach it later.
- **His legwork:** it can't do his store checks, channel checks or franchisee conversations. His
  Restaurant Brands loss came from that kind of missed signal.
- **The size limits** are this desk's rule.
- **The consensus score** follows readers' summaries of his book and counts headlines, not
  analysts' quotes.
- **No trades:** it never trades and never sizes positions. The scorecard shows how its ideas did.

## Settings

Under `trends:` in `config.yaml`:

| Setting | Default | What it does |
|---|---|---|
| `chatter` | `true` | Turns the Bluesky chatter counter on or off |
| `chatter_sample_s` | `600` | Seconds of firehose read each hour |
| `chatter_ratio` | `3.0` | How far above its baseline a brand's share must rise |
| `chatter_min_mentions` | `30` | Mentions needed across the 3 days |
| `earnings` | `true` | Reads next report dates from Nasdaq's calendar |
