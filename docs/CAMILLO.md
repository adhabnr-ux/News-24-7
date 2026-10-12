# The Camillo desk

Chris Camillo's method, social arbitrage, runs as rules over the trend lane
([TRENDS.md](TRENDS.md)). It has its own tab in Foretape, the **Camillo** page, and its own API,
`GET /api/camillo`.

**Sources.** Everything the page says about him is sourced in `news247/data/camillo.yaml`:
interviews (including the Iced Coffee Hour episode of Aug 9 2026), his posts, Jack Schwager's
*Unknown Market Wizards* chapter, and his book *Laughing at Wall Street*. Quotes are as those sources print them. The research couldn't open the book texts
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
low-conviction ones if he could do it again. For the last couple of years he has traded those at
2-3% of a full size instead (Aug 2026).

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
| Hear the other side | Each idea lists the headlines arguing against it, and fails a test when a shock (an outbreak, a recall, a lawsuit, a ban, a cease-and-desist, a force majeure, an executive exit, a guidance cut) has hit the brand since the evidence began |
| Watch the narrative | **The attention trade:** each market narrative's share of every headline read, and of Bluesky posts, against its baseline; an alert when one starts accelerating |
| Exit at parity | Each idea shows its exit: consensus score above 2, the report, the evidence stopping, or a shock breaking the thesis. Unsure: sell half. Every idea is scored against SPY at 30/60/90 days |

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
| Nothing new has broken the thesis | No shock headline about the brand since the evidence began. For a short idea a shock is the thesis playing out, so it passes |

Each idea also carries:
- **Conviction:**
  - **High** when the idea is HIGH and passes all 8 tests. His high-conviction trades came a few times a year, after 50-100 hours of research.
  - **Low** otherwise. His rule for those: trade 2-3% of a high-conviction size, or not at all.
  - It's a label: the desk never sizes a trade.
- **The other side:** up to three headlines arguing against the idea (gloom for a long, hype for a short), one per source. He says he spends more time on the other side than on his own thesis.

### The attention trade

> "I'm really paying attention more so than ever to the attention trade, meaning like where people
> have their attention, even if the information is not truthful." He wants to measure "the
> acceleration of that narrative because if it starts to accelerate quickly then that is a signal,
> especially if we get into a levered state again, that we could see a massive correction."
> (Iced Coffee Hour, Aug 9 2026)

His miss in July 2026: he knew the Kimi K3 open model was coming but under-weighted how fast that narrative
was spreading. Kimi K3 came out on Jul 16, and Bloom Energy fell 13.6% that day.

**What a narrative is.** `data/camillo.yaml` lists the narratives. Each is a set of words, and a headline or post belongs to it
when it says one of `any` and, if set, one of `with`. The five, from his account of 2026:

| Narrative | Side | Example words |
|---|---|---|
| Cheap open models make compute less needed | fear | Kimi K3, DeepSeek, Qwen, open-weight, Chinese AI model |
| AI spending won't pay back | fear | AI bubble, capex, overbuild, circular deals (with an AI word) |
| AI labs can't pay for their compute | fear | OpenAI, Anthropic, xAI with cash burn, debt, default, bailout, backstop |
| Leverage unwinding | fear | margin call, deleveraging, forced selling, unwind (with fund, retail, Korea, leverage…) |
| Companies cutting costs with AI | hope | AI with cost savings, customer-service costs, efficiency, headcount, job cuts |

**How it's measured** (`camillo.narrative_pulse`):
- **The share:** the narrative's share of *everything* read each day (every headline this system stores; Bluesky posts sampled). A busier news day doesn't look like a narrative.
- **The comparison:** the last 3 days against the median of the 14 before.
- **Levels:**
  - **Accelerating:** 2x or more (`narrative_ratio`), with at least 8 matching headlines (`narrative_min`), and the latest day not below the first of the three, so it's still rising.
  - **Elevated:** at least 1.5x, or 2x but fading.
  - **Quiet** otherwise; **learning** until there is enough history.
- **Thin days are skipped:** a day with fewer than 20 headlines read doesn't count. On Oct 12 2026 a sample of NVDA and AMZN headlines showed why: weekend days of 17-18 headlines made two "AI bubble" headlines look like 4.8x. The rule above calls that quiet, which is right.

**Alerts** (`NarrativeWatch`, checked every 30 minutes):
- **MEDIUM** (in the app) when a narrative starts accelerating.
- **HIGH** (pushed) when two or more fear narratives in the same sector accelerate together: his fear cycle forming.
- **Once per wave:** one alert per narrative per 3 days (`narrative_cooldown_days`).
- **What the alert carries:** the counts, the sources, the names each narrative hits most (this desk's list), and the latest headlines.
- **Also shown:** in the morning brief (`brief.narratives`) and in the page's **The attention trade** section.

### His views, dated

The page shows his theses from the Aug 9 2026 interview, each with his quote and our own price
check since then (Aug 7 to Oct 9 closes; SPY +0.7%). They are his views as dated, not this desk's calls:

| View | Since (Aug 7 → Oct 9) |
|---|---|
| The Amazon trade isn't over | AMZN −4.4% |
| Still heavily in Bloom Energy | BE +27.9% |
| The AI efficiency wave (Booking.com's AI customer-service savings) | BKNG −25.2% |
| No SpaceX at $1.6T | SPCX +22.1% |
| The Sweetgreen wrap thesis is on hold until the outbreak passes | SG +78.3% |

He also argued that distribution moats win once intelligence is free (the coffee-cup theory), that
another AI fear cycle would come within 30-90 days, and that AI labs are too big to fail.

## The page

1. **Live ideas:**
   - every signal from the last 45 days, strongest first;
   - its evidence and links;
   - the checklist with the numbers behind each test;
   - the exit plan.
2. **Heating up:** brands drawing unusual Wikipedia attention or Bluesky chatter that isn't a signal
   yet.
3. **The attention trade:** every narrative's level, its ratio to normal, its sources, and the
   latest headline.
4. **His views:** his dated theses, with our price check since.
5. **The method:**
   - his principles, with quotes and sources (15, seven of them from the Aug 2026 interview);
   - the ten-step process, mapped to this desk;
   - what he avoids;
   - his record as reported;
   - what this desk can't do;
   - what's said about him without a source.
6. **Case studies:** 15 of his trades, each with a source:
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
   - Restaurant Brands (a loss);
   - Amazon and AWS, from developers on Reddit (the early trade);
   - Amazon's Q2 2026 call, the $10 million day (AMZN +15.3% on Jul 31 2026);
   - Bloom Energy and the forced sellers (−44.5% from Jul 6 to Jul 29 2026, then +26.5% the day
     Situational Awareness's book was sold);
   - Sweetgreen's wraps, overtaken by the cyclospora outbreak.

   Where we checked the stock's daily bars ourselves, the "price check" line says so. It isn't his
   profit.
7. **Track record:** how this desk's ideas did against the S&P 500.
8. **The feeds:** when each source was last read.

## What it can't do

- **The platforms he reads:** he reads TikTok, Instagram and X, which have no free API. The desk
  reads Bluesky, Wikipedia and the App Store instead, so TikTok-only trends reach it later.
- **His legwork:** it can't do his store checks, channel checks or franchisee conversations. His
  Restaurant Brands loss came from that kind of missed signal.
- **The size limits** are this desk's rule.
- **The consensus score** follows readers' summaries of his book and counts headlines, not
  analysts' quotes.
- **Narrative words:** the narratives are word lists. A rebuttal ("AI isn't a bubble") counts as
  attention to the narrative, which is how he frames it: where attention is, true or not. The list
  of names each narrative hits is this desk's.
- **Thesis checks:** the thesis check reads headlines, not filings, and a flagged shock is a prompt to
  check, not a verdict. Amazon's ban on AI shopping agents (Oct 2026) is flagged on an AMZN idea
  because it bears on his distribution-moat thesis, not because it ends it.
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
| `narratives` | `true` | The attention trade (narratives from `camillo.yaml`) |
| `narrative_ratio` | `2.0` | How far above its baseline a narrative's share must rise |
| `narrative_min` | `8` | Matching headlines needed across the 3 days |
| `narrative_cooldown_days` | `3` | One alert per narrative per wave |
