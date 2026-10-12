# Far-out-of-the-money options on big stocks' directional days

*Study run Oct 12 2026 (in session) on real OPRA and stock 5-minute bars from TradingView.
It answers four questions:*
1. *How many days out to buy?*
2. *At what price, and when, to buy?*
3. *How do you know the volume wave is over?*
4. *Does a spiked contract that falls back toward $0.01 make a good entry?*

*The rules it led to are in [DAYMOVE.md](../DAYMOVE.md).*

## The setup

> Options across all big stocks have high volume depending on the direction of the day. If MSFT is
> bullish on a day, some call $50–$60 out of the money goes crazy; if it is bearish, some put
> $50–$60 under goes crazy.

MSFT trades around $540, so $50–$60 out is about 10% out of the money. The study looks at three
groups:

- **10%-out contracts** on the days the stock had its move.
- **The same strike across every expiry** on one day, to isolate days to expiry.
- **Days whose opening move failed**, as controls. Without these, every rule looks good: the
  trend days were picked after the fact.

## Data

| Group | Days | Contracts (5-minute OPRA bars) |
|---|---|---|
| Trend days | META Sep 21 (+11.3%), Sep 24 (+4.5%), Sep 28 (−4.8%), Oct 12 (+4.1%, to ~13:20 ET); MSFT Sep 25 (+3.7%); TSLA Oct 2 (+4.7%); MU Oct 12 (−3.7%); AMD Oct 12 (−2.9%) | META $790 call at 0, 2, 4, 11, 18 and 39 days; META $750 call (0 days); META $735 call (25 and 60 days); META $820 call (22 days); META $675 put (18 days); MSFT $550 call (21 days); TSLA $390 call (14 days); MU $990 put (0 days); AMD $550 put (4 days) |
| Failed opens (controls) | TSLA Sep 25 (gap +2.4%, close −1.5%); NVDA Oct 9 (gap +1.5%, close −0.5%); MSFT Oct 1 (gap +1.4%, close 0%) | TSLA $415 call (21 days), NVDA $255 call (7 days), MSFT $565 call (15 days) |

Plus the stock's 5-minute bars for every day.

Limits:

- TradingView serves no expired contracts, so past days can only be studied through expiries
  still open on Oct 12.
- The sample is 11 days and 18 contracts. The numbers below are case studies with a consistent
  direction, not statistics.
- Fills assume the 5-minute close plus 5%, standing in for the spread. Cheap contracts often
  cost more than that.
- Oct 12 figures stop at about 13:20 ET, not the close.

## 1. Days to expiry: the same strike on the same day

META, Oct 12 2026. Yesterday's close was $718.67; the stock was up 4.1% (to $752 at its high).
The contract is the $790 call, 9.9% out of the money.

| Days out | Yesterday's close | Day's high (x) | ~13:20 (x) | Bought at 9:45, to ~13:20 |
|---|---|---|---|---|
| 0 | $0.03 | $0.10 (3x) | $0.03 (1x) | not in play: META never got near $790 |
| 2 | $0.17 | $1.92 (11.3x) | $1.28 (7.5x) | **+54%** (worst −49%) |
| 4 | $0.43 | $3.50 (8.1x) | $2.65 (6.2x) | **+29%** (worst −31%) |
| 11 | $1.93 | $7.25 (3.8x) | $6.57 (3.4x) | +10% (worst −23%) |
| 18 | $9.50 | $18.07 (1.9x) | $17.80 (1.9x) | +1% (worst −16%) |
| 39 | $16.84 | $27.45 (1.6x) | $26.00 (1.5x) | −1% (worst −10%) |

What the table shows:

- **The month-out contracts had repriced in the first 15 minutes.** Bought from yesterday's close,
  the 18-day call made +90% and the 39-day call +55%. Bought at 9:45, they made about 0% for the
  rest of the day. A ~1-month 10%-out contract pays only if you own it **before** the move, or if
  the trend keeps going for hours or days.
- The days that did keep going show that second case:
  - META Sep 21 (+11.3%, an all-day trend): the 25-day $735 call bought at 9:45 made +141% to the
    close and was +103% the next day. The 60-day made +64% and +49%.
  - META Sep 24 (+4.5%): the 22-day $820 call was **−45%** the next morning (META −3.3%).
  - META Sep 28 (−4.8%): the 18-day $675 put was **−39%** the next day (META +3.2%).
- **The biggest multiples are on 0–2-day contracts whose strike the stock actually reaches.** The
  META $750 call expiring that day (4.4% out) went 38x at its high. The $790 (9.9% out) expiring
  the same day did nothing.

Same-day rankings across the other trend days, from yesterday's close to the day's high:

| Day | Contract | Days | Out of the money | Day's high (x) | Close (x) | Next day (x) |
|---|---|---|---|---|---|---|
| META Sep 21 +11.3% | $735 call | 25 | 10.4% | 5.9x | 5.4x | 4.5x |
| META Sep 21 +11.3% | $735 call | 60 | 10.4% | 2.6x | 2.5x | 2.3x |
| META Sep 24 +4.5% | $820 call | 22 | 10.2% | 1.75x | 1.65x | 0.72x |
| META Sep 28 −4.8% | $675 put | 18 | 10.2% | 2.1x | 2.0x | 1.07x |
| MSFT Sep 25 +3.7% | $550 call | 21 | 10.5% | 4.8x | 3.7x | 2.0x |
| TSLA Oct 2 +4.7% | $390 call | 14 | 10.1% | 2.6x | 2.0x | 2.8x |
| AMD Oct 12 −2.9% | $550 put | 4 | 9.6% | 2.0x | 1.5x (13:20) | — |

**Answer to "how many days out":**
- **For the day's move, 2–7 days at roughly 0.25 delta.** That captured the most of the move
  after a confirmed call: +54% / +29% on META.
- **For a trend that may carry over, 14–35 days at roughly 0.30 delta.** These held value
  overnight (the META $735 call was still +103% the day after Sep 21). They captured little of a
  one-day move bought after the open, and the next day can take 40% off when the stock gives
  part of its move back.
- **Picking by delta beats picking a fixed $50–$60 or 10% out.** 10% out is a different bet at
  2 days (needs a ~10% move) than at 30 days (needs a few percent and some time). Delta prices
  the distance, the time and the stock's volatility together.

## 2. When to buy: five entry rules, on trend days and failed opens

| Rule | Trend days: median to the close | Up | Median worst | Failed opens |
|---|---|---|---|---|
| **At the open** (first 5-minute close) | **+30%** | 11/15 | −5% | **−24%, −47%, −57%; MU's 0-day put −83%** |
| **Confirmation** (1.5% and through the first 15 minutes' range) | +3% | 8/14 | −23% | never triggered (no trade) |
| **After the first-hour volume climax, a 30% pullback** | −1% | 3/6 | −45% | −32%, −48% |
| **"Back to a penny"** (80% off a 3x+ high) | +148% | 2/3 | −23% | only on 0-day contracts (below) |
| **Late, at 5x yesterday's close** (when a broker shows +400%) | +17% | 4/5 | −35% | — |

What the table shows:

- **Buying at the open** works when the day trends and loses big when the opening move fails.
  The open is the right price only if the direction is already known.
- **Waiting for a breakout confirmation** avoids every failed open. It comes so late (11:05 on
  META) that the option has already repriced.
- **The 9:45 hold** sits between the two. At 9:45 the stock must be 1%+ from yesterday's close
  and at least as far out as its first 5-minute close (not fading). It skipped all three failed
  opens and MU's bounce. It took 11 trend-day trades: median **+10%** to the close, 7 of 11 up,
  median worst −17%.

The production rule adds two parts:
- **Holding near the extreme:** the stock is within 10% of its best move of the day.
- **After 10:00:** a call when the stock is pressing its extreme, which is how META Sep 24 got
  called at 10:05.

Replayed on all 11 days:

| Day | Call | Then |
|---|---|---|
| META Oct 12 | calls 9:49 (+3.7%) | pullback zone 11:24 |
| META Sep 21 | calls 9:49 (+5.7%) | zone 11:04; close +10.7% |
| META Sep 24 | calls 10:04 (+2.4%, pressing its high) | zone 11:09; close +4.5% |
| META Sep 28 | puts 9:49 (−4.0%) | zone 12:24; close −4.8% |
| MSFT Sep 25 | calls 9:49 (+3.5%) | zone 10:04 (a sharp dip that recovered); close +3.65% |
| TSLA Oct 2 | calls 9:49 (+4.2%) | zone 11:24; close +4.7% |
| AMD Oct 12 | puts 10:04 (−2.2%) | zone 11:44 |
| MU Oct 12 | puts 10:04 (−4.4%) | zone 10:59, **faded 11:59** (MU bounced to −2.9%) |
| TSLA Sep 25 | puts 9:49 (−1.4%, the reversal) | zone 10:19, **faded 12:04** |
| NVDA Oct 9 | no call | close −0.5% |
| MSFT Oct 1 | no call | close 0% |

Options bought at those times, on the contracts in the data:
- **At the call:** median **+14%** to the close, 7 of 11 up.
- **At the pullback zone:** median **+14%**, 8 of 11 up, usually with shallower drawdowns. On
  MSFT Sep 25, the zone at 10:04 bought the $550 call at about $2.40; it reached $3.60.
- **The two big losses were MU's 0-day put** (−78% from the call, −61% from the zone). The desk's
  default picks start at 1 day out.

**Answer to "what price":** the ask when the call is made, or a limit inside the **pullback zone**.
The zone is the contract's value with the stock back at 85% to 70% of its move, at the same
implied vol. The alert computes both prices for each contract.

## 3. Is the volume wave over?

The study found 41 option volume climaxes: 5-minute bars at 4x+ the contract's median volume and
5%+ of its day's volume.

- **8 of 41** were within 10 minutes of the contract's high of the day. **29 of 41** saw a higher
  price later. A volume spike alone does not mark the top.
- **What decided the outcome was how much of its move the stock still held at the climax:**

| The stock at the climax | n | Made a new high later | Higher at the close | Median to the close |
|---|---|---|---|---|
| 90%+ of its best move | 24 | 18 | 14 | +4% |
| 70–90% | 7 | 4 | 5 | +14% |
| under 70% (or the wrong way) | 6 | 3 | **0** | **−16%** |

So the desk watches the stock, not the option's volume, to call the end:
- **Under 70% of the move held for 15 minutes:** the call is off. The 15 minutes matter because
  MSFT Sep 25 dipped to 68% at 10:14 for 10 minutes, then closed +3.65%.
- **Option volume** is shown as context, on Alpaca's free feed with trades about 15 minutes late:
  building, still heavy, past its peak, or dried up.

Climaxes after 15:00 (META Sep 28, TSLA Oct 2) had no follow-through. There are no new calls
after 15:00.

## 4. "Back to a penny" after the spike

On trend days, contracts 2+ days out gave back a **median of about 20%** from their high of the
day. The most was 67%, on the 2-day META call. **None went back to pennies.**

The collapse to cents happened only on **contracts expiring that day**, and only when the stock
gave its move back:
- **MU $990 put:** $15.30 at 9:30 → $0.90 at 12:05, as MU climbed from −4.8% to −2.6%.
- **META $750 call:** $4.88 → $0.76 within the session, as META chopped around $745–$752.

From those lows, each re-ran when the stock re-tested its extreme:
- the MU put to $2.89 (+220%);
- the META call to $5.72.

So a penny entry is a bet that the stock re-tests its extreme in the hours left, on a contract
that goes to $0 at 16:00 otherwise. It shows as a separate **lotto** tier, off by default.

## What the published research says

These citations were checked only through search listings, not by opening each paper. Figures
marked (secondary) come from summaries.

- **Option volume does carry information, but mostly the part the public can't see.**
  - Pan & Poteshman (2006, *RFS* 19(3)): buyer-initiated, position-opening put/call ratios predict
    returns of 40+ bp the next day and 1%+ over the next week. The publicly observable part of the
    signal does not.
  - Johnson & So (2012, *JFE* 106(2)): a high option-to-stock volume ratio is bearish on average
    (0.34% a week, lowest vs highest decile).
  - Augustin, Brenner & Subrahmanyam (2019, *Management Science*): abnormal short-dated
    out-of-the-money call volume before about 25% of takeovers.
  - Jiang & Strong (SSRN 3618427, working paper): unusual activity reported on CNBC sees "an
    immediate overreaction … followed by subsequent reversal."
- **Far-OTM and short-dated options lose money for buyers on average.**
  - Boyer & Vorkink (2014, *JF* 69(4)): a strong negative link between an option's lottery-like
    skewness and its average return.
  - Ni (working paper): calls more than 15% out average about −28% (secondary).
  - Bryzgalova, Pavlova & Sikorskaya (2023, *JF* 78(6)): retail buyers favor cheap weeklies, pay
    about 12.6% spreads and lose money on average.
  - The desk is not a "buy lottery tickets" rule. It buys only after the stock has shown its
    direction, and it has an exit.
- **Intraday momentum is real but weak, and runs into the close when dealers are short gamma.**
  - Baltussen, Da, Lammers & Martens (2021, *JFE* 142(1)).
  - Gao, Han, Li & Zhou (2018, *JFE* 129): R² about 2% (secondary).
- **Overpaying for implied vol, and holding overnight, cost option buyers.**
  - Goyal & Saretto (2009, *JFE* 94).
  - Muravyev & Ni (2020, *JFE* 136(1)): option returns are negative overnight.
- **No published test of an options "volume exhaustion" signal was found.** Equity work splits
  high-volume moves into those that continue (information) and those that reverse (liquidity):
  Llorente, Michaely, Saar & Wang (2002, *RFS* 15(4)).

## Conclusions

1. **The trade is the stock's direction; the option is the lever.** Call the day at 9:45 (or
   when the stock is pressing its extreme), not when the option is already +400%.
2. **Days out, by what's left of the move:**
   - **2–7 days, ~0.25 delta** for the day.
   - **14–35 days, ~0.30 delta** for a trend that may carry over.
   - **Not 10%-out 0–2-day contracts:** they need a 10% move.
   - **Not month-out contracts bought after the open on a 3–5% day:** they had already repriced.
3. **Price:** the ask at the call, or a limit in the pullback zone (the stock at 85–70% of its
   move).
4. **The end:** the stock under 70% of its move for 15 minutes. The option's volume climax is
   context, not the signal.
5. **"Back to a penny"** only happens on contracts expiring that day, and is a re-test bet.
6. **Exit by the close unless the stock closes at its extreme.** Next-day results were mixed:
   −45% and −39% on two of the trend days.
