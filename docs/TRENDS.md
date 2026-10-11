# The trend lane: what people are doing, before the earnings report says it

Chris Camillo turned a small account into a large one by noticing what people were buying
before Wall Street did. The trend lane (`news247/trends.py`) is that method with criteria a
machine can hold to, and a scorecard that says whether it works.

## What edge is actually left

- **Not secret information.** Hedge funds already buy the professional versions of this data:
  foot traffic (Placer.ai), app downloads (Sensor Tower), card spending (Second Measure,
  Earnest, Consumer Edge). For a big, heavily covered company, the data is in the price within
  days.
- **What is left is narrower:**
  1. **Mapping speed.** Tying a trend to the right *listed* company, fast. "Pokémon cards are
     selling out" is a GameStop story; "Hoka is everywhere" is a Deckers story.
  2. **Materiality.** The brand has to be big *for the company*. A viral product inside a $3T
     company moves nothing.
  3. **Data vs price.** The rarest and best case: the data says one thing and the stock does
     the other. The earnings report settles who was right.

  Examples from the research of Oct 11 2026:
  - **QSR:** Burger King visits +7.1% year on year (Placer.ai) while QSR fell 9% in a month
    (results Oct 29).
  - **YUM:** Taco Bell visits −12.2% while management called the trend "improving" (results
    Nov 3).
  - **GME:** Pokémon-card demand was real, but GME had already risen 39% in a month, so it was
    priced in.
- **"Zero gaps" isn't possible.** Some trends never show up in a free public feed, and a trend
  can be real while the stock still goes the other way. What the lane does guarantee:
  - every signal is filtered by the same written rules;
  - every signal is recorded with the price at the time;
  - every signal is scored against the S&P 500 30, 60 and 90 days later.

  The edge is measured, not assumed.

## The four kinds of evidence

| Kind | Feed | The test | Direction |
|---|---|---|---|
| **Attention** | Wikipedia page views for the brand (Wikimedia's free API), read once a day after 06:00 ET | The 7-day average is **2x+** the median of the 8 weeks before, **4+ robust standard deviations** up (median absolute deviation, floored at 5% of the median), on **4 of the last 7 days**, at **1,500+ views a day** | None: a scandal draws attention too |
| **App Store climb** | Apple's free top-200 US free-apps chart, read every 6 hours | Inside the **top 50** for **3 snapshots in a row**, after being **outside the top 150 (or off the chart)** two weeks earlier | Up |
| **Tracker data** | Any news source: a headline naming a tracker (Placer.ai, Sensor Tower, Circana, Second Measure, YipitData, Similarweb…) with a customer metric; or a mapped brand with a customer metric ("Taco Bell's traffic still down double digits") | A metric (visits, traffic, downloads, installs, active users, card spend) that points somewhere. Earnings releases are excluded ("same-store sales", "quarterly results"): that is the report itself, not a lead on it | From the numbers: rose, +7%, up double digits / fell, −12%, slows |
| **Sell-out / waitlist** | Any news source | Sold out, waitlist, out of stock, restock, shortage, backorder, "can't keep … in stock", with a consumer word (shoppers, fans, stores, launch, collectors…) | Up, unless the headline says otherwise |

**Why each test is shaped this way:**
- **The 4-of-7-day rule** drops one-day news spikes. A CEO resignation sends a page up 20x for
  one day; a trend keeps it up.
- **The robust z-score** drops pages that swing every day anyway.
- **The two-week lookback** on the App Store means a chart regular can't "climb". A new app only
  counts as "off the chart" once the chart has been watched without a break for two weeks.
- **Wikipedia titles are checked every day.** Each title in the brand map is resolved through
  Wikipedia's own API (normalization and redirects) before its views are read. A redirect's own
  page views are near zero, so reading one would silently hide the brand. The desk reports:
  - titles with no article (`missing_articles`);
  - titles that are really another article (`redirected_titles`);
  - articles averaging under 50 views a day (`quiet_articles`).

## Who it maps to

`news247/data/trends.yaml` maps about 50 brands to their listed owners. The weight follows a
rule anyone can check, not an estimated revenue share:
- **core:** the brand *is* the company (Chipotle, Duolingo, Celsius);
- **major:** one of a company's few headline brands (Burger King at Restaurant Brands, Hoka at
  Deckers).

Apps that aren't in the map still count when their developer's name matches a listed company
exactly. Those count as **auto**.

To add a brand:
1. Add one line with `brand`, `ticker`, `weight`, `wiki` (article titles), `apps` (words in the
   app's name or developer) and `terms` (how headlines name it).
2. Run `news247 trends --check` on the server to confirm the titles resolve.

## The filters (all required before anything reaches you)

1. **Listed common stock.** The ticker must be in the company list (`news247 universe`).
   Private companies, warrants and units are dropped.
2. **Material:**

   | Weight | Company size limit |
   |---|---|
   | core | $150B |
   | major | $60B |
   | auto | $20B |

3. **Not priced in.** The stock has *not* already moved **15%** in the trend's direction since
   the evidence began (Yahoo daily closes). For attention with no direction, a 15% move either
   way means the market noticed. A stock moving *against* the trend is the opportunity, not a
   reason to drop it.
4. **Consistent.** Evidence pointing both ways is dropped as mixed.
5. **Fresh.** Evidence combines inside a **14-day** window. There is **one signal per company
   per 30 days**, unless a new one is stronger.

## Severity

| Severity | When | Where it goes |
|---|---|---|
| **HIGH** | Two or more independent kinds agree on a direction, and every filter passes | Pushed |
| **MEDIUM** | One kind with a direction, or attention alone | The app's feed, not pushed |
| **LOW** | Too big to matter, size unknown, priced in, or mixed | The trend board only (`news247 trends`, `/api/trends`), never pinged |

Each trend alert carries:
- every piece of evidence, with its link;
- the company's size and the brand's weight;
- the stock's move since the evidence began;
- the reasons behind its severity.

The morning Brief lists the week's MEDIUM and HIGH trend signals.

## The scorecard

Each signal is stored with its price. Once a day after 07:00 ET it is marked at **30, 60 and 90
days**: the stock's return, SPY's return and the difference.
- **A hit:** the stock beat SPY in the signal's direction. For direction-free attention, a hit
  is any 5%+ difference.
- **The scorecard reports:**
  - the hit rate and the average return vs SPY, by severity and horizon;
  - for down signals, the short side's return, so it is counted as positive.

```bash
news247 trends              # the board: latest signal per company, its evidence, filters, marks
news247 trends QSR YUM      # just these companies
news247 trends --check      # read every feed once now: titles, attention vs baseline, charted apps
news247 trends --json
```

`GET /api/trends?days=30` returns:
- the board;
- the scorecard;
- the lane's health (articles read, failures, missing, redirected or quiet titles, apps
  tracked, the brands drawing the most attention right now).

## Configuration

Under `trends:` in `config.yaml` (see `news247/data/config.example.yaml`):

- **Every threshold above.**
- **`scan: false`** stops the Wikipedia and App Store polling and keeps news evidence only.
- **`enabled: false`** turns the lane off.

## What it does not do (yet)

- **TikTok, Instagram, Google Trends:** no free, stable, server-friendly API. Google Trends
  blocks automated reads, and TikTok's research API is for academics. Their effect still shows
  up one step later, in Wikipedia attention, the App Store chart and sell-out headlines.
- **Paid panels (Placer.ai, Sensor Tower, Second Measure):** read only when the press reports
  their numbers.
- **The build machine can't reach Wikimedia or Apple's feed.** Every parser is tested on the
  real response shapes, and Render can reach both. `news247 trends --check` on the server is the
  live confirmation, and it lists any title that needs fixing.
- **No trades.** Trend signals are leads to research. The scorecard decides whether they deserve
  more trust.
