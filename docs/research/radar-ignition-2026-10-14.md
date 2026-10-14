# Catching the move at the start: the radar on real tape (Oct 14 2026)

**Question:** the radar alerts when a stock is already 4–35% from yesterday's close. Can a rule
on the live tape (price, volume and the day's range) see the same moves earlier, early enough to
trade? And when is such a signal worth acting on?

**Answer:**
- **The radar's ladder fires late.** After it fires, the stock more often goes back than on.
- **Ignition sees the big moves earlier.** "Ignition" is a short push on several times the
  normal volume to a new session high or low, with a slower "build-up" trigger beside it.
  Together they saw 33 of 39 big intraday moves first: a median 22 minutes before the radar, with
  45% of the move left (19% at the radar).
- **It doesn't pick winners on its own.** Across all its signals, ignition was close to a coin
  flip by the close.
- **So it pushes only when something backs it:** a headline, an insider buy, or options volume
  on the same side. Everything else goes on the board, and its option chain is read at once.

## Data

| | |
|---|---|
| Stocks | 106 US stocks. A seeded random sample from TradingView's screener: 10 mega caps ($200B+), 25 large ($10–200B), 25 mid ($2–10B) and 30 small ($300M–2B), plus 16 names from recent audits (NVAX, PCVX, FICO, SMTC, CBRS, FIGS, FNMA, FMCC, OPCH, HUM, XP, RXO, MU, IREN, ...) |
| Bars | TradingView 1-minute bars, regular session, Sep 21 – Oct 13 2026 (17 sessions). Minutes with no trade are forward-filled |
| Sessions judged | 1,455 symbol-days. Each needs 5+ earlier sessions for its normal volume: the mean of up to the 10 before it |
| Split | Rules set on Sep 28 – Oct 5 (821 symbol-days), checked on Oct 6 – 13 (634) |
| Entry | The close of the minute **after** the signal (one minute of delay). Outcomes are in the signal's direction |

**Normal volume for the time of day.** The median share of a session's volume traded by each
minute, measured on these 1,455 sessions, is in `news247/data/volume_profile.json`:

| By | 09:45 | 10:00 | 10:30 | 11:00 | 12:30 | 14:00 | 15:00 | 15:55 | 16:00 |
|---|---|---|---|---|---|---|---|---|---|
| Share of the day | 6% | 9% | 17% | 24% | 41% | 57% | 69% | 88% | 100% |

The last five minutes, with the closing auction, carry 12% of the day. A stock that has traded
10% of a normal day by 09:45 is running at about 1.7× normal.

## The radar as it is

The radar fires when the day move crosses its line: 20% for small caps, 10% mid, 6% large, 4% mega.

| After the radar fired (68 cases) | |
|---|---|
| Closed the day further in the move's direction | 37% |
| Return to the close, median / mean | −0.59% / −0.93% |
| One hour later, mean | −0.53% |
| Its "push" line (1.5×, 20 cases), one hour later | −1.54% mean, 35% further |

These moves mostly had no news in the sample. That is the pattern Chan (2003, "Stock price
reaction to news and no-news", *Journal of Financial Economics*) found over decades: large moves
without news tend to reverse, while moves with news drift on. Radar alerts with no headline now
say so.

## Ignition and build-up

**Ignition** (all must hold):
- **The move:** in about 5 minutes the stock moved 3% (small cap), 1.5% (mid), 1.0% (large) or
  0.6% (mega).
- **The volume:** 4×+ the volume normal for that stretch of the day.
- **The level:** a new session high (low).
- **Still early:** the day move before the push was under the radar's line.

**Build-up:** the day move is half-way to the radar's line, at a new session extreme, on 2×+ the
day's normal volume so far. This catches the grinding moves, like FIGS +12.5% on Oct 12, that
never burst.

### What it caught (big intraday moves)

A big intraday move is a session that reached the radar's line without gapping there: the open
was under half of it. There were 39 such sessions in the sample.

| Rule | Saw it before the radar | Lead over the radar (median) | Move left when it fired (median) |
|---|---|---|---|
| The radar's line | — | — | 19% |
| Ignition alone | 27 of 39 | 8 min | 35% |
| Ignition or build-up (study) | 33 of 39 | 22 min | 45% |
| **As shipped** (`market/ignition.py`, the bars replayed as live ticks) | **28 of 39** | **20 min** (26 earlier, 1 later) | **40%** |

As shipped:

| Day | Stock | Move | Ignition | Radar |
|---|---|---|---|---|
| Oct 5 | NVAX | +27.8% (plague scare) | 13:46 | 14:47 |
| Oct 12 | FNMA | −19.9% (Burry) | 9:37 | 9:44 |
| Oct 12 | FMCC | −18.2% | 9:35 | 9:42 |
| Oct 12 | CBRS | −11.2% (chip selloff) | 9:47 | 9:51 |
| Oct 12 | META | +4.7% | 9:37 | 10:47 |
| Oct 12 | FIGS | +13.9% (no headline) | 10:08 (build-up) | 11:55 |
| Oct 8 | SNDK | −6.4% | 9:52 (build-up) | 12:49 |
| Sep 28 | TSLA | −4.1% | 9:51 (build-up) | 15:52 |
| Oct 2 | RXO | +12.5% | 10:08 (build-up) | 12:09 |
| Oct 12 | FICO | +11.1% on normal volume | — | 11:55 |

Missed, 11 of 39:
- **Moves on normal volume (7).** FICO (both days), MU on Sep 28, CBRS on Sep 28, PCVX on Oct 9,
  META on Sep 28 and SNDK on Oct 2 traded 0.6–1.1× their normal volume. Without unusual volume
  there is nothing for a volume rule to see.
- **Moves already near the line when they burst (4).** SMTC crossed its line by 9:40 on Oct 2; on
  Oct 12 it was down 5% by 9:35, before a 5-minute window could form. PM on Oct 8 and PTC on Oct 1
  made their burst only after they were already near it.

### What a signal is worth on its own

Over every ignition, not just the ones that became big days:

| Production-like rule (135 signals, 0.09 per stock per day) | 5 min | 15 min | 30 min | 60 min | Close |
|---|---|---|---|---|---|
| Further in its direction | 57% | 58% | 55% | 56% | 47% |
| Mean return | +0.22% | +0.41% | +0.48% | +0.37% | 0.00% |
| Set-up weeks (Sep 28 – Oct 5) | 55% | 52% | 43% | 45% | 38% |
| Check weeks (Oct 6 – 13) | 59% | 64% | 67% | 68% | 56% |

Within the first hour, the median best point was +1.3% and the median worst −1.0%.

So:
- **The follow-through comes early.** It arrives in the first 15–60 minutes, and by the close it
  is mostly given back.
- **It depends on the week.** The two halves of the sample disagree, which is what a signal
  without information looks like.

Splits (all 135 signals; small samples, read as hints):

| Split | n | Further by the close | Next session |
|---|---|---|---|
| The day's volume 3–6× normal | 46 | 57% (median +0.65%) | 59% |
| ... under 1.5× | 21 | 38% | 45% |
| 09:35–10:00 | 87 | 52% | 51% |
| After 10:00 | 48 | 38–42% | 38–47% |
| Against the day so far (a reversal) | 38 | 63% | 59% |
| With the day so far | 97 | 41% | 44% |

The build-up trigger alone was no better: 40% further by the close, n = 65.

Market-wide this is a lot of signals:
- **Every ignition:** about 0.07–0.09 per stock per day, roughly 500 a day across 5,500 stocks.
- **"Strong" ones** (the day's volume 3×+ normal and the push 8×+): about 40 a day.

## What ships

`market/ignition.py` and `market/tape.py`:

- **The tape.** Every stock of $100M+ is read every 30 seconds in the session.
  - **With Alpaca keys:** Alpaca snapshots (price, the day's volume, range and VWAP), on the same
    request budget as the options tape.
  - **Without keys:** Yahoo quotes, once a minute.
  - Every volume comparison is made within one feed. On the free plan, today's IEX volume is
    compared with a normal IEX day.
- **Pushed (HIGH, up to 10 a day):** ignitions with something behind them.
  - **A headline:** since the last session's midpoint.
  - **An insider:** an open-market purchase in the last 3 days (Form 4).
  - **Options:** volume on the same side (calls for a move up) in the last 3 hours, including
    the chain read the ignition itself triggers.

  This follows the literature: news-backed moves drift (Chan 2003), and option volume leads the
  stock (Easley, O'Hara & Srinivas 1998; Pan & Poteshman 2006).
- **Primed.** When options are bought on a stock that hasn't moved, when an insider buys, or when
  a headline names it, the stock goes on watch for 2 hours (insiders: 3 days). Its first push
  then needs only 60% of the move and 3× the volume, and the alert says why.
  - This is the "options before the stock" case: the options alert says *"The stock hasn't
    moved yet: it is on watch"*, and the push that follows is alerted at once.
- **Shown, not pushed (up to 40 a day):** strong ignitions with nothing behind them yet, marked
  *"Nothing backs it yet"*.
- **On the board only:** the rest. Their option chain is read at once, and a flow hit there
  confirms them.
- **Every ignition alert gives:**
  - the launch price,
  - where the move has failed (past the launch or the session VWAP, whichever is nearer) and how
    far that is,
  - the volume against normal,
  - what backs it,
  - what the study says about how long the follow-through lasts.

## Limits

- **2.5 weeks, 106 stocks:** 135 ignitions and 39 big intraday days. The lead time and the share
  of the move left are robust across the sample. The win rates move from week to week, which is
  why confirmation is required to push.
- **Regular session only.** Pre-market and after-hours moves are the radar's sweep's job. The
  ignition profile is a regular-session profile.
- **IEX volume.** On Alpaca's free plan the tape sees IEX trades, a few percent of the market.
  Ratios within one feed hold up; very thin names are skipped (fewer than 5,000 shares on a
  normal day on the feed).
- **Confirmation is not tested here.** The sample had no news or options history. That piece
  rests on the published research above, and on the scorecard going forward.
- **Not advice.** The system never trades.

Scripts (not shipped): the study ran from `load.py`, `study.py`, `study2.py`–`study4.py` and
`replay_prod.py` over the saved TradingView bars. The shipped scanner is checked against six of
those sessions in `tests/test_ignition.py` (`tests/fixtures/ignition_days.json`).
