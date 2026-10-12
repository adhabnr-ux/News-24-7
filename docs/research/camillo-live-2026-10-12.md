# The Camillo desk, live (Oct 12 2026): ideas now, and his Aug 2026 interview

Two parts:
1. The desk's method run today, with its 8 tests, on every candidate we could find evidence for.
2. A full read of Chris Camillo's Iced Coffee Hour interview of Aug 9 2026, checked against the
   record, and what it changed in the desk.

**How this was run.**
- Production (Render) can't be queried from the build machine, so the desk's own code
  (`camillo.consensus_score`, `thesis_check`, `other_side`, `narrative_pulse`) was run here.
- The inputs:
  - TradingView headlines (up to 200 per name);
  - TradingView prices (Oct 9 closes) and report dates;
  - four research passes.
- The research passes could only read search-result summaries, because the build proxy blocks
  article pages. Every figure from them is marked *(snippet)*: open the source before relying on
  it.
- The trend lane's automatic feeds (Wikipedia, the App Store chart, Bluesky) can't be read from
  here either. Without them no idea can show two independent kinds of evidence, so everything
  below is MEDIUM at most.

**These are research leads, not trades.** Nothing was traded. Each idea is graded after its report
(table at the end).

---

## Part 1: live ideas

### The verdict

**No HIGH idea today.** No candidate passes all 8 tests on evidence we can check. The best are
MEDIUM, and by his own rule they are low conviction: worth 2-3% of a full size, or nothing.

| # | Idea | Report | Tests passed | Conviction |
|---|---|---|---|---|
| 1 | **Hyatt (H): the luxury hotel boom, long** | Oct 29 (confirmed by Hyatt) | 7 of 8 (one kind of evidence) | Low |
| 2 | **Robinhood (HOOD): bets moving from sportsbooks to prediction markets, long** | Oct 27 after the close (confirmed) | 6-7 of 8 (one kind; the Ohio order to check) | Low |
| 3 | **Cava (CAVA): fast casual beating fast food, long** | Nov 10 | 6 of 8 (one kind; brand data stale) | Low, verify first |

### 1. Hyatt (H): long into Oct 29

**Ground data (CoStar/STR, US hotels):**
- In the week of Sep 27-Oct 3, luxury and upper-upscale RevPAR rose 17.8%, the strongest of any
  segment, and RevPAR rose in 86% of markets. *(snippet)*
- US RevPAR by week: +14.2% (Sep 20-26) and +12.9% (Sep 27-Oct 3). *(snippet)*
- By month: July +8.2%, August +2.0%, September month to date +11.2%. That makes Q3 roughly +7%.
- [CoStar, Oct 2026](https://www.costar.com/article/1555135087/us-hotels-strongest-stretch-of-2026-rolls-on)

**The mapping:** Hilton and Marriott own the headlines, but Hyatt is the purer play on the segment
growing fastest.
- Its Q2 beat was "on luxury, upper upscale hotels demand" (Reuters headline, Jul 30).
- It raised its RevPAR outlook (Zacks, Aug 5).

**The 8 tests:**

| Test | Result |
|---|---|
| Something new | Yes |
| Two kinds of evidence | **No.** Tracker data only |
| Points one way | Yes, up |
| Material | $15.3B, and the brand is the company |
| Imbalance | Headlines are mostly neutral: target raises (Mizuho $222, Oct 8) against "weaker international tourism to the U.S." (Oct 5) |
| Price | $161.94: +1.2% over 1 month, −15.1% over 3 months. Not caught up |
| Catalyst | Oct 29 before the open |
| Thesis | No shock |

**The other side:**
- CoStar itself credits part of the jump to the Yom Kippur calendar shift.
- Las Vegas was the only top-25 market down on all three measures.
- International travel to the US is weaker.
- Hyatt cut its 2026 net rooms growth in July, and the stock fell on it.

**Graded:** right if Q3 comparable systemwide RevPAR is at least +5% or the full-year RevPAR
outlook is raised again.

### 2. Robinhood (HOOD): long into Oct 27

**Ground data:**
- **Prediction markets set records in September and October.**
  - Over the NFL Week 4 weekend: $9.43B across prediction markets, including $3.68B at Kalshi on
    Sunday alone ([Covers, Oct 5](https://www.covers.com/industry/kalshi-records-new-volume-highs-all-trading-surpasses-9-billion-oct-5-2026)). *(snippet)*
  - Kalshi and Polymarket did a record $22.2B in a week ([Bloomberg, Oct 11](https://www.bloomberg.com/news/articles/2026-10-11/prediction-markets-nfl-boom-tests-sports-betting-rules)). *(snippet)*
  - More than 80% of Kalshi's September volume was sports.
- **Sportsbooks are losing that money.** New York mobile-betting revenue for the first three NFL
  weeks was **−28% year over year**
  ([Covers, Oct 2](https://www.covers.com/industry/new-york-sports-betting-handle-declines-during-nfls-week-3-oct-2-2026)). *(snippet)*
- **Robinhood's own data** (Sep 1-24, released Sep 28): 4.4B event contracts, about 234M
  options contracts, about $267B equity notional and about $17B crypto notional *(snippet)*.
  Through Sep 24, Q3 was up on Q2: options +10%, event contracts +12%, crypto +13% (our note of
  Oct 11).
- **Consensus Q3 revenue is $1.418B,** only 5.7% above Q2.

**The 8 tests:**

| Test | Result |
|---|---|
| Material | $98B, under the $150B limit for a brand that is the company |
| Two kinds of evidence | **No.** Company and industry data only |
| Imbalance | The desk's score on 44 Robinhood headlines in 30 days: **0.75** (3 hype vs 4 gloom sources), an imbalance |
| Price | $109.02, **−9.7% over 1 month**: against the trend |
| Catalyst | Oct 27, confirmed |
| Thesis | **Check:** Ohio sent Robinhood and nine others cease-and-desist notices over sports event contracts on Oct 2, deadline Oct 16 *(snippet)*. The desk now flags this kind of headline. |

**The other side** (the desk's list):
- Robinhood Chain transactions fell more than 40% as memecoin trading cooled (Oct 10).
- ARK trimmed Robinhood as Bitcoin slid (Oct 7).
- The Sep 1-24 run rate puts equity volume about flat with July and options below July.

**Graded:** right if Q3 revenue is at least $1.45B.

### 3. Cava (CAVA): long into Nov 10, verify first

**Ground data:**
- **Category level only.** In Placer's September index, fast casual visits rose 2.2% year over
  year while fast food fell 2.2% (its 7th straight monthly decline)
  ([Placer.ai](https://www.placer.ai/anchor/articles/september-2026-retail-and-dining-index-shoppers-keep-showing-up-despite-record-gas-prices)). *(snippet)*
- **The latest brand-level read is old:** Cava same-store visits +8.2% in June.
- **Camillo's own observation (Aug 9):** Cava "is more positioned for the masses" and is "a really
  tough competitor" for Sweetgreen.

**The 8 tests:**

| Test | Result |
|---|---|
| Material | $6.3B, core |
| Price | $53.64: −10.1% over 1 month, −21.3% over 3 months. Against the trend |
| Imbalance | Turning: target cuts (JPMorgan, RBC, Sep 17-18), then "Analysts turn bullish on … Cava" (Oct 9) and Melius at $95 (Oct 8) |
| Thesis | No brand shock. But a Seeking Alpha piece on Oct 9 says confidence in food safety is at a new low, an industry risk |
| Catalyst | Nov 10 |

**What it needs:** a September brand read for Cava (Placer's Q3 recaps are due). Until then, this
is a lead, not an idea.

**Graded:** right if Cava beats its Q3 same-restaurant-sales consensus.

### Checked and dropped today

| Name | Why |
|---|---|
| **YUM** (Taco Bell short, #1 on Oct 11) | **Downgraded.** The −12.2% traffic came from the record cyclospora outbreak (iceberg lettuce), which the press covered heavily and the CDC declared over on Sep 11. Gloom-heavy coverage is parity for a short, and once a shock is the whole story the original thesis is irrelevant (his Sweetgreen lesson). |
| **DKNG** short | Fails "the price hasn't caught up": −18.2% in a month, past the 15% line. The Wedbush data (DraftKings daily active users −7%, FanDuel +9%) stands. |
| **FLUT** long | Fails the new thesis test: Brazil banned online betting on Sep 28 ("Flutter Stock Sinks After Brazil Bans Online Betting", Barron's). Its Brazil exposure needs checking before any FanDuel share thesis matters. |
| **QSR** | Burger King's Dragon Ball week was +20.1%, but that compares a week without Labor Day to one with it, and the fast-food category fell 2.2% in September. One kind of evidence; TD Cowen cut its target on Oct 7. Watch only. |
| **HLT** | Same STR data, but $73.5B and +8.2% over 1 month; Hyatt is the purer exposure. |
| **HPQ** | IDC: Q3 PC shipments −20% (Oct 9). But HP warned of an extended slump and withheld FY27 guidance on Sep 21: the story is told. |
| **MAT** | KPop Demon Hunters is a top-3 Halloween costume, but MAT is +26% over 3 months: priced in. |
| **AEO** | *Off Campus* is the top Halloween costume theme and American Eagle sells an online-only merch line, but no sales or share figures were found. Watch. |
| **TTWO** | GTA VI (Nov 19) is known to everyone: parity. |
| **SG** | +39% over 1 month and +78% since Aug 7: priced in. Camillo's wrap thesis is "relevant" again now the outbreak is over (FDA closed its probe Oct 2), but the stock already moved. |
| **CELH, ELF** | Alani Nu (about 47% of Celsius's Q1 revenue) and Rhode (about 33% of e.l.f.'s quarter) are material, but the data comes from their own reports, so it's public. |
| **DPZ** | Reports Oct 13 before the open; no ground data. |

### The attention trade, today

The new narrative measure was run over two TradingView headline samples:

- **Bloom Energy + Nebius** (393 headlines, Aug 12-Oct 8): **none of the five narratives
  accelerating.** No headline about cheap open models or AI capex doubt appeared in their feeds
  in two months.
- **Nvidia + Amazon** (388 headlines, Sep 30-Oct 12):
  - **"AI spending won't pay back":** 12 headlines in 13 days, 1-3 a day. It includes Ray Dalio's
    bubble warning (Oct 8, Oct 11) and rebuttals (DBS on Oct 5; AWS's CEO on Oct 9).
    - A loose reading put it at 4.8x, but that came from weekend days of 17-18 headlines.
    - Under the production rule (at least 8 matches in 3 days, at least 20 headlines a day) it is
      **quiet**.
  - **"Cheap open models":** 2 headlines (DeepSeek nearing a $12B round; Reflection's model).
  - **Leverage and AI-lab credit:** nothing.
- **Reading:** no fear cycle is forming in these samples.
  - On Aug 9 he expected one within 30-90 days (by about Nov 7).
  - The desk now measures that continuously over every headline the system reads, not two
    samples.

One thing the samples did surface for his Amazon thesis: **Amazon is blocking AI shopping agents.**
- "Amazon's Ban on AI Agents Is the Opening Its Rivals Need" (Barron's, Oct 8).
- "Amazon Stocks Fall as Muse Ban Tests Its Shopping Moat" (GuruFocus, Oct 9).

That bears directly on "distribution moats win". If AI agents become the shopping front door, the
moat is the thing being tested. The desk's thesis check flags it on an AMZN idea.

---

## Part 2: the interview

**Iced Coffee Hour, "EMERGENCY UPLOAD: Stock Market Investors Are Losing Everything - How To
Profit!", published Aug 9 2026** ([YouTube](https://www.youtube.com/watch?v=ahQlC54rqhk)).
- Recorded in the week after Amazon's Jul 30 report: he says Sweetgreen reports "this week", and
  it reported Aug 6.
- The transcript was read in full.

### What happened, checked against the record

Prices are TradingView daily closes unless marked.

| Date | Event | Check |
|---|---|---|
| Jun 22 | BE's closing high, $345.85 (intraday high $351.28 on Jun 25) | Bars |
| Jul 8 | Hunterbrook short report on Bloom; BE −5.7% to $254.29 | Bars and [Yahoo](https://finance.yahoo.com/markets/stocks/articles/bloom-drops-5-7-short-123148391.html) |
| Jul 16 | Kimi K3 (open weights) released; chip stocks enter a bear market; **BE −13.6%** | Bars and [Decrypt](https://decrypt.co/373736/kimi-k3-triggered-deepseek-flashbacks-stock-market) |
| Jul 21 | Korean margin loans down to 33.4T won from 38.6T (Jun 24); KOSPI −22% in July | [Bloomberg](https://www.bloomberg.com/news/articles/2026-07-21/korean-stock-traders-unwind-leveraged-bets-to-lowest-since-april) and [Fortune](https://fortune.com/2026/08/02/kospi-rout-korea-stock-market-volatility-sk-hynix-samsung-ai-leveraged-etf/) *(snippet)* |
| Late Jul | Situational Awareness margin called at about 4x gross leverage | [CNBC, Jul 31](https://www.cnbc.com/2026/07/31/why-leopold-aschenbrenner-situational-awareness-hedge-fund-imploded.html) *(snippet)* |
| Jul 27 | Camillo on X: "If it tanks, I'll be buying more" | [X](https://x.com/ChrisCamillo/status/2081794583432679863) |
| Jul 28 | Bloom reports record revenue ($1.065B) and raises guidance; BE still **−11.3%** to $166.84 | Bars and [SEC](https://www.sec.gov/Archives/edgar/data/0001664703/000162828026050150/ex991_q226financialresults.htm) |
| Wed Jul 29 | BE closes at **$163.75** (intraday $157.33); his 24-hour options expire worthless | Bars |
| Thu Jul 30 | The fund sells its roughly $16B public book to Citadel; **BE +26.5%**; Amazon reports after the close; his X post: "Most gut-wrenching, highest-conviction, levered trade of my life… We did it." | Bars and [X](https://x.com/ChrisCamillo/status/2082925083631272364) |
| Fri Jul 31 | **AMZN +15.3%** ($235.50 to $271.58): his "$10 million day" | Bars |
| Aug 3 | AMZN closes at a record $284.02; market cap tops $3T | Bars |
| Aug 6-7 | Sweetgreen cuts its outlook; SG −8.0% to $5.40 | Bars and [CNBC](https://www.cnbc.com/2026/08/06/sweetgreen-cuts-full-year-outlook-as-cyclospora-fears-weigh-on-sales.html) |
| Aug 9 | Interview published | |

**His claims, one by one:**

| He said | The record |
|---|---|
| Bloom "went from $300 a share to 165… cut by 45%" | **True.** $295.05 (Jul 6) to $163.75 (Jul 29) is −44.5%; from the $345.85 closing high it is −52.7%. |
| "The fastest growth for AWS in nearly 5 years" | AWS revenue about +37% to $42.2B against about 31% expected *(snippet)*. His "22% guide / 25% whisper" figures were not found. |
| Jassy would raise capex to 220 and say most of it can be pulled back | Capex was raised to about $220B ([CNBC](https://www.cnbc.com/2026/07/30/amazon-amzn-q2-earnings-report-2026.html)), with Jassy saying AWS still won't have enough capacity in 2026 *(snippet)*. The pull-back wording wasn't found in the sources read. |
| Leopold was "4x levered" and margin called | Reported (CNBC). His "$40 billion" fund size wasn't confirmed; the public book sold was about $16B. |
| "Citadel made two to two and a half billion dollars" | **Unverified.** Reported instead: Citadel bought the book, and its flagship fund had its best month since 2022 ([CNBC, Aug 5](https://www.cnbc.com/2026/08/05/ken-griffins-citadel-posts-best-month-in-years-after-scooping-up-situational-awareness-stocks.html)). Listed under `unverified` in the playbook. |
| "All of Korea was getting margin called" | Directionally true: margin loans fell about 13% in three weeks and KOSPI fell 22% in July. |
| SpaceX at "1.6 trillion… after being down 40%" | Consistent. IPO at $135 ($1.77T); intraday high $225.64 (Jun 16); closes of $108-115 on Aug 3-6 are about $1.5-1.6T at today's share count (our arithmetic). |
| Sweetgreen's slump came from a "stomach virus and lettuce" | The outbreak was real (iceberg lettuce from a Mexican facility). **Sweetgreen was not implicated** and says it uses no iceberg lettuce; the hit was fear (about 600 basis points off July comps). His point stands: the outbreak was the only thing driving the stock. |
| Booking.com's AI is cutting customer-service costs | **True.** The CFO said in Feb 2026 that cost per reservation fell about 10% while bookings rose about 10% ([CFO Dive](https://www.cfodive.com/news/booking-uses-ai-to-slash-customer-service-costs-cfo-says/812624/)); in August, that it keeps falling at a double-digit rate *(snippet)*. |
| $20K to about $80M; 70-80 trades in 17 years; 60-70% a year; 160 private deals at 10-12% | Self-reported. The audited record (Schwager) runs to 2020: 77% a year. |

### What's new in his method, and what the desk now does with it

| His rule (Aug 2026) | On the desk now |
|---|---|
| **The attention trade:** measure how fast a narrative is accelerating, true or not, especially when the market is levered | `Narrative`, `narrative_pulse`, `attention_trade`, `NarrativeWatch`: five narratives measured over every headline read and over Bluesky. MEDIUM alert when one accelerates; HIGH when two fear narratives move together. Shown on the page and in the brief. |
| **Never marry a stock:** exit in an instant when the thesis breaks (Sweetgreen) | `thesis_check`: an 8th test. A shock headline about the brand since the evidence began (an outbreak, a recall, a lawsuit, a ban, a cease-and-desist, a force majeure, an executive exit, a guidance cut) fails a long idea. |
| **The other side first:** more time on the bear case than the bull case | `other_side`: every idea lists up to three headlines arguing against it, one per source |
| **Small when unsure:** 2-3% of a high-conviction size | `conviction`: each idea is labeled high or low with his rule (a label: the desk never sizes a trade) |
| **Buy half, sell half** | In every exit plan: "Unsure? Sell half." |
| **Ground truth, not noise:** find the one driver, then go to the people doing the work (developers on Reddit and tech forums) | A principle and a case (AWS, from developers on forums). The desk can't read Reddit or X; that stays a stated limit. |
| **Probabilities and scenarios:** 80/20, known and unknown unknowns | A principle, with the Amazon trade as the case |
| **Know where the leverage is:** forced selling isn't information, and it ends | A principle, a case (Bloom, Jul 2026) and the "Leverage unwinding" narrative |
| **Avoid:** loyalty to stocks, copying trades, the trade after a big win, paying for what others see, illiquid money | Five new entries under "What he avoids" |

### Our read of it (the Axelrod view)

- **The $10M trade wasn't social arbitrage.** It was an event-driven options bet on how a CEO
  would handle a call, sized with leverage in the middle of a drawdown. The ground-truth work
  (100+ hours) set the probability; the timing came from the calendar. The desk keeps the part
  that generalizes, *the catalyst is the moment the market learns*, and doesn't copy the leverage.
- **His drawdown was a crowding lesson.** His edge names (Bloom, Nebius) were also a levered
  fund's names and Korean retail's names. When their margin calls came, everyone holding them got
  the same forced selling. "Know where the leverage is" is the same advice as "know who else owns
  it". The new "Leverage unwinding" narrative is the desk's version.
- **The record since Aug 7** (to Oct 9; SPY +0.7%) is mixed:
  - AMZN −4.4%: "the trade isn't over" hasn't paid since.
  - BE +27.9%.
  - BKNG −25.2%: the efficiency-wave example.
  - SPCX +22.1%: his pass has been wrong on price so far.
  - SG +78.3%: "on hold" missed the rebound.

  The method says exit at parity and judge ideas by their reports, not weeks of price, but this is
  why the desk scores everything against SPY instead of trusting a name.
- **Self-reported numbers stay labeled as such:** the $10M day, the 40% drawdown and the $80M.

---

## Grading after the reports

| Idea | Graded on | Right if |
|---|---|---|
| H | Q3 comparable systemwide RevPAR / full-year outlook | At least +5%, or the outlook raised |
| HOOD | Q3 revenue | At least $1.45B (a beat of 2% or more) |
| CAVA | Q3 same-restaurant sales vs consensus | A beat |
| YUM (dropped) | Taco Bell US Q3 comp | Record only: was the outbreak the whole story? |
| The attention trade | Any narrative reaching "accelerating" before Nov 7 | Record the date, and the AI names' move vs SPY over the next 10 days |

Every idea is also marked against SPY at 30, 60 and 90 days.
