# Day moves: a big stock's directional day, and the options to buy

When a big stock ($50B+) is having its directional day, the options tape sends one alert with:

- which side to buy (calls on an up day, puts on a down day);
- which contracts, and **the exact prices**;
- **the stock level that ends the trade**.

After that come a pullback-zone alert, a fade alert, and the usual follow-ups on the contract.

The rules come from a study of real OPRA tape. It covered 7 trend days and 3 days whose opening
move failed (Sep 21 – Oct 12 2026): [research/options-volume-strategy-2026-10-12.md](research/options-volume-strategy-2026-10-12.md).

## The call

| When | Rule |
|---|---|
| **9:45–10:00 ET: the 9:45 hold** | The stock is 1%+ from yesterday's close (`daymove_min_pct`), at least as far out as its first 5-minute close (not fading), and within 10% of its best level of the day (`daymove_hold`) |
| **10:00–15:00: pressing its extreme** | The stock is 1%+ from yesterday's close and holds 90%+ of its best move of the day |
| **After 15:00** | No new calls (`daymove_until`) |

- One call per company per day, at most `daymove_daily` (10) a day, biggest companies first.
- The prices come from the live store (Alpaca snapshots every 60 s, the radar's Yahoo sweep every
  minute). When a call is made, the company's chain is read at once.

On the study days, the rule:
- called META, MSFT, TSLA and AMD on their trend days;
- called MU and TSLA (Sep 25) on moves that later faded, and the fade line caught both;
- made no call on NVDA Oct 9 or MSFT Oct 1, whose opening pops failed.

## The contracts

| Pick | Days to expiry | Target | For |
|---|---|---|---|
| **Day trade** | 1–7 | delta nearest 0.25 | the rest of today's move |
| **Trend (multi-day)** | 14–35 | delta nearest 0.30 | a trend that may carry over to the next days |
| Lotto (off: `daymove_lotto`) | 0–1 | delta nearest 0.15 | a re-test of the day's extreme; goes to $0 at 16:00 otherwise |

- Each pick must have a live two-sided market no wider than 25% of its mid (`daymove_max_spread`)
  and be worth $0.10+.
- Picks are chosen by delta, not a fixed distance. A 10%-out contract is a different bet at 2 days
  than at 30. In the study, on META's +4% day, the same 10%-out call bought at 9:45 made:
  - **+54%** at 2 days out;
  - **+29%** at 4 days;
  - **+10%** at 11 days;
  - **+1%** at 18 days;
  - **−1%** at 39 days.
- The month-out contracts had repriced in the first 15 minutes.

Each pick shows:
- the ask and bid;
- delta and implied vol;
- $ per 1% the stock moves its way;
- breakeven at expiry;
- **the pullback buy zone**: the contract's value with the stock back at 85% and at 70% of its
  move, at the same implied vol.

## The alerts

```
📊 META day move: calls · META up 3.7% and holding (9:45 hold) · Meta Platforms · $1.9T mega cap
META $745.20 (+3.69%): best $745.20 today, yesterday $718.67. Called at 09:50 ET (9:45 hold).
• Day trade: META $770 call (Oct 16) (4d): ask $5.70 / bid $5.47 · delta 0.26 · +$2.20 per 1% META moves (40%) · breakeven at expiry $775.70
  Pullback buy zone $4.62–$3.78 (with META $741.22–$737.24) · implied vol 45% · 100 traded
• Trend (multi-day): META $810 call (Nov 13) (32d): ask $17.94 / bid $17.24 · delta 0.30 · +$2.32 per 1% META moves (13%) · breakeven at expiry $827.94
  Pullback buy zone $16.43–$15.33 (with META $741.22–$737.24) · implied vol 45% · 100 traded
How to use it: buy at the ask now, or rest a limit inside the pullback zone. The call is off if META stays under
$737.24 (70% of its move) for 15 minutes. After you buy, the follow-ups say when it runs, fades or hits its stop.
```

*Engine output for META's real 9:45 price on Oct 12 2026 ($745.20, yesterday $718.67). The chain
is **modeled** (Black-Scholes at 45% implied vol, the test chain in `tests/test_daymove.py`), so
the option prices are not real quotes.*

| Alert | When | Severity |
|---|---|---|
| 📊 **day move** | the call: its chain is read and the picks are priced | HIGH |
| 🔁 **pullback zone** | the stock pulls back to 85% of its move, still above 70%: the chain is re-read, the picks are repriced, and their option volume shown (building / still heavy / past its peak / dried up) | MEDIUM |
| ⛔ **faded** | the stock stays under 70% of its move for 15 minutes (`daymove_fade`, `daymove_fade_s`): no new entries | MEDIUM |

- The day pick of a call or zone alert is then followed like any alerted contract
  ([OPTIONS.md, "After the alert"](OPTIONS.md)).
- Follow-ups: 📈 still running, ⚠️ fading, 🛑 stop hit, 🔁 running again.

## Why these rules (from the study)

- **The direction call is the trade.**
  - Bought at the open, 10%-out contracts made a median +30% by the close on trend days, and lost
    24–83% on days whose opening move failed.
  - Waiting for a breakout confirmation avoided the failed days, but only made a median +3%.
  - The 9:45 hold skipped all the failed opens. It took 11 trades: median +10%, 7 of 11 up.
  - Options bought at the production rule's calls made a median +14% (7 of 11 up). At its
    pullback zones, +14% (8 of 11 up).
- **Option volume climaxes don't mark the top; the stock does.**
  - Only 8 of 41 climaxes were within 10 minutes of the contract's high.
  - With the stock holding 90%+ of its move at the climax, 18 of 24 made new highs later.
  - Under 70%, 0 of 6 closed higher.
- **"Back to a penny" only happened on contracts expiring that day**, when the stock gave its move
  back (MU's $990 put: $15.30 → $0.90). Contracts 2+ days out gave back a median of about 20%
  from their high and never went to pennies.
- **Overnight is a coin flip.** The next day, month-out contracts ranged from −45% to +103%. The
  follow-ups end at the close.

These are 11 days and 18 contracts: case studies with a consistent direction, not statistics.
The published research says out-of-the-money option buyers lose on average (Boyer & Vorkink
2014; Bryzgalova, Pavlova & Sikorskaya 2023). The desk exists to make the direction call, the
price and the exit explicit, not to make lottery tickets a good bet. **The system never trades.**

## Settings (`options.*`)

| Setting | Default | Meaning |
|---|---|---|
| `daymove` | `true` | the desk on/off (`OPTIONS_DAYMOVE`) |
| `daymove_min_cap` | `50e9` | companies this big |
| `daymove_min_pct` | `1.0` | the stock this far from yesterday's close |
| `daymove_hold` | `0.9` | ... holding this much of its best move |
| `daymove_fade` / `daymove_fade_s` | `0.7` / `900` | the call is off under 70% held for 15 minutes |
| `daymove_until` | `"15:00"` | no new calls from this time (ET) |
| `daymove_daily` | `10` | calls a day at most |
| `daymove_day_min_dte` / `max` / `delta` | `1` / `7` / `0.25` | the day-trade pick |
| `daymove_swing_min_dte` / `max` / `delta` | `14` / `35` / `0.30` | the trend pick |
| `daymove_lotto` / `daymove_lotto_delta` | `false` / `0.15` | the 0–1 day pick |
| `daymove_max_spread` / `daymove_min_price` | `0.25` / `0.10` | the pick's market quality |

`GET /api/status` → `options.daymove` shows today's calls, their picks and events.
