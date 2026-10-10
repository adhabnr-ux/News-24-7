# The options tape

Foretape reads the full option chain of every listed US company worth **$1B or more**, from
$1B up to the largest. It pings you in two cases:

1. **A contract up thousands of percent today.** This means 1,000% or more above its previous
   close, on any strike, any expiry, calls or puts.
2. **Unusual volume.** This means contracts trading far above their open interest, with real
   money behind them.

Both alerts land in the same feed and the same push channels as the news and the radar.

```text
🔥 NWE $75 call (Oct 16) +2,167% today · NorthWestern Energy · $4.6B mid cap
$0.15 → $3.40 (bid $2.90 / ask $3.60) · 288 contracts ($98K) · NWE $73.97 (+7.33%)
9 days to expiry · strike 1.4% out of the money
Cboe delayed quotes (~15 min) · quotes as of 11:42 ET. Option prices move fast; check the live bid/ask.

🐋 Unusual options: XYZ $1.2M · XYZ $55 call (Oct 16) 6,000 contracts, 12× open interest · Xylo Zinc · $5.0B mid cap
$1.4M in unusual contracts (calls $1.2M / puts $195K) · XYZ $52.00 (+4.00%)
XYZ $55 call (Oct 16): 6,000 contracts vs 500 open · $1.2M at $2.05, near the ask (likely bought)
Side is estimated from where the last print sat in the bid/ask spread.
```

*(The first example uses the real NWE prices from Oct 7 2026; the bid and ask shown in it are
made up, since daily bars don't record them. The second example is synthetic.)*

## How a "thousands of percent" spike is checked

A contract that is "up 5,000%" is often an artifact. A study of 30 "lottery ticket" call alerts
posted on social media (Sep 29 to Oct 9 2026) found that most screenshot badges were measured
from a stale **$0.01** "previous close" on a thinly traded strike. Measured from the actual
entry, the same trades were +30% to +100%. So every spike is tested before it can push:

| Test | Rule (default) | Why |
|---|---|---|
| Up enough | last ≥ previous close × 11 (`spike_min_pct: 1000`) | "thousands of percent" |
| Traded today | the last print is from today's session | an old "last" is not a move |
| Real size | ≥ 25 contracts, last ≥ $0.10, ≥ $5,000 traded | a $0.01 → $0.11 print is noise |
| **Bid-confirmed** | the bid now is still ≥ half the threshold above the base | you could actually sell; it is not one print in an empty book |
| **Real base** | the previous close is ≥ 30% of the contract's model value that day | catches the stale $0.01 base |

The model value is Black-Scholes at yesterday's stock price, the time to expiry then, and the
company's 30-day implied vol. If the previous close is stale, the move is re-measured from the
model value and the alert says so: "The $0.01 previous close looks stale (model value that day
$0.32); the % is measured from the model value." A spike that still clears 1,000% from the model
value is real and pushes. One that doesn't is shown in the app as **unconfirmed**, with the
reason, and is not pushed.

**Expiry day.** Contracts expiring today (0DTE) routinely multiply on ordinary moves, especially
for large companies on Fridays. They show in the app from 1,000% but push only from 3,000%
(`spike_0dte_multiple: 3`; set it to 1 to treat them like any other contract). Without this, a
handful of routine same-day spikes would use up the daily push limit before noon.

Each company fires once per rung per day: 1,000%, 2,000%, 5,000%, 10,000% and 25,000%. An
unconfirmed spike that later confirms is upgraded to a push. Restarts remember what already
fired (`data/options_state.json`).

### Replay: NWE, Oct 7 2026 (real bars)

`tests/test_options.py::test_replay_nwe_oct_7_from_real_bars` uses TradingView's OPRA and Nasdaq
daily bars (`tests/fixtures/options_nwe_2026-10-07.json`):

- **The stock:** $68.92 → $73.97 (+7.3% on 4× volume). Its 20-day realized vol was 16.6%, the
  same as the study's 16%.
- **The 75 call (Oct 16):** it last traded at $0.15 on Sep 25, then traded up to **$3.40** on
  Oct 7 and closed at $1.10.
- **At the high:** +2,167%. The base is real: the model value was $0.03 and $0.15 is above it.
  It crosses the 2,000% rung.
- **At the close:** +633%, under the line, so no alert.
- **Bid:** daily bars carry none, so the replay can only show the spike as unconfirmed. The
  live feed has the bid.
- **Black-Scholes check:** it matches the study's numbers ($0.00 at 16% vol, $0.03 at 26%).
- **Open interest (Alpha Vantage):** the 75 call traded **36×** its open interest, so only 8
  contracts were open before the day. The 80 call traded 157.5×. That is real unusual activity,
  but only about $32K changed hands in the 75 call. That is under the $100K-per-contract floor,
  so it raises no volume alert; the spike rule is what flags it. Lower
  `flow_min_contract_premium` if you want mid-cap volume this small to count.

## Unusual volume

A contract is unusual when **all** of these hold:

- at least 500 contracts traded today;
- at least 3× its open interest (any amount when open interest was 0);
- at least $100K traded in it (contracts × price × 100).

A company is reported when its unusual contracts add up to **$250K or more**. The alert gives:

- the premium in calls vs puts;
- the top contracts, with volume against open interest;
- where each contract's last print sat in the spread ("near the ask, likely bought"). This is
  an estimate; the feed has no trade-by-trade side.

It pushes at **$1M or more** when the biggest contract is at least 5× its open interest. A
company is reported again only when its unusual premium doubles.

## Coverage and speed

- **Which companies:** every common stock in the universe (Nasdaq's screener, the same one the
  small-cap lane uses) worth `min_market_cap` ($1B) or more, about 3,000. A company with no
  listed options is skipped for a day.
- **Hot names, re-read every 3 minutes (up to 40 at a time):**
  - companies moving 3% or more today;
  - names on the price radar's board;
  - any company in a news or price alert, for 2 hours.
- **Everyone else** rotates through, largest first. At ~3 chain reads a second, a full pass takes
  about 15 to 20 minutes.
- **When:** the regular session only. Option quotes don't move outside it.
- **Push limit:** at most 12 options pushes a day (`daily_pushes`). The rest are in the app.

## The data, and its limits

**Two sources, one fallback.** With Alpaca keys set (`ALPACA_KEY`, `ALPACA_SECRET`), chains come
from **Alpaca** and Cboe takes over any read Alpaca fails. Without them, Cboe alone. Setup,
feeds, rate limits and the read-only Alpaca MCP server are in [ALPACA.md](ALPACA.md). Every
alert names the feed it came from. The rest of this section is about Cboe.

**Source:** Cboe's public delayed-quotes JSON, one request per company, every expiry and strike:
`https://cdn.cboe.com/api/global/delayed_quotes/options/<SYMBOL>.json`

**Fields used:**
- per contract: `option` (OCC symbol), `bid`, `ask`, `last_trade_price`, `prev_day_close`,
  `volume`, `open_interest`, `iv` and `last_trade_time`;
- for the stock: `current_price`, `prev_day_close`, `percent_change`, `iv30` and
  `last_trade_time`.

The field names match OpenBB's open-source Cboe provider (`openbb-cboe`), which reads the same
endpoint.

- **Delayed about 15 minutes.** Every alert says so and gives the quote time.
- **Stale chains are skipped.** A chain is stale when its stock quote lags the newest quote the
  feed has seen by more than 30 minutes. It raises no alert and is counted on `/api/options`.
  The check is relative on purpose: if Cboe stamps Central rather than Eastern time, an absolute
  check would mark every chain stale and silence the tape.
- **You are told when it breaks.** One system alert a day if the feed fails 10 reads in a row
  (for example, Cboe blocking the server), or skips 25 chains in a row as stale. A broken feed
  is never mistaken for a quiet market.
- **Not a documented API, and Cboe's terms restrict automated extraction.** Cboe's delayed-quote
  pages say automated extraction is prohibited and that Cboe may block IP addresses that do it.
  The feed is paced at about 3 requests a second and backs off on errors and 429s. Even so, Cboe
  could block your server or change the format at any time. If that happens, the tape goes quiet
  and its status shows the errors; nothing else in Foretape is affected. A licensed feed (Polygon
  options, Tradier, ThetaData) can be swapped in at `chain_url` and `parse_chain`.
- **Unverified from the build environment.** The live endpoint wasn't reachable from the sandbox
  where this was built. The parser is tested on the field names above, and the rules on real NWE
  bars. **The first thing to run on your server is:**

  ```bash
  news247 options NVDA NWE AAPL
  ```

  For each symbol it prints the contracts it read, the quote time, the biggest movers today
  (previous close → last, bid/ask, volume, open interest) and any alert it would raise. Add
  `--min-pct 200` to see the rules fire on a quieter day.
- **What it can't see:** trade-by-trade direction (the side is estimated), block versus sweep
  prints, and prices between two reads of the same chain. A spike that rises and falls back
  between reads is caught only if a read lands near the peak. For hot names, reads are 3
  minutes apart.

## Settings

All settings are under `options:` in `config.yaml`; each is commented in
`news247/data/config.example.yaml`. These can also be set with environment variables:

| Variable | Default | Setting |
|---|---|---|
| `OPTIONS_ENABLED` | `true` | turn the tape on or off |
| `OPTIONS_MIN_CAP` | `1000000000` | smallest company covered |
| `OPTIONS_SPIKE_PCT` | `1000` | spike threshold, in % |
| `OPTIONS_FLOW_PUSH` | `1000000` | unusual premium that pushes |
| `OPTIONS_DAILY_PUSHES` | `12` | most options pushes per day |

**To turn it off:** set `OPTIONS_ENABLED=false`, or `options: {enabled: false}`. Nothing else
changes.

## Where to see it

- **Your phone:** HIGH alerts push like any other alert. The app shows every options alert under
  the source label **OPTIONS**.
- **`GET /api/options`:** the latest hit per company, plus the feed's health: companies covered,
  chains read today, rotation position, stale chains, errors and back-off.
- **`GET /api/status`:** the same health, under `options`.

## Files

- `news247/market/options.py`: the parser, Black-Scholes, the spike and volume rules, and the
  feed.
- `news247/engine.py`: alert text and wiring (`on_options`, `_options_alert`, `options_board`).
- `news247/config.py`: `OptionsConfig`, every threshold with a comment.
- `tests/test_options.py`: 55 tests, covering pricing, parsing, every rule, the feed (pacing,
  priorities, back-off, freshness, problem reports), the engine, the API, the CLI and the NWE
  replay.
