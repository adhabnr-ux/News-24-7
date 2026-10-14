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

### Options before the stock

When a stock has barely moved (under half the radar's day line), unusual volume, a spike or a
blowup on one side means something different. The alert then says *"The stock hasn't moved yet
(+0.3%): it is on watch, and its first push up on volume alerts at once."*

For the next 2 hours the stock is **primed** on the ignition scanner (the live tape,
[SMALLCAPS.md](SMALLCAPS.md#ignition-the-move-at-its-start)). A push in the options' direction
then needs only 60% of the usual move and 3× normal volume. It is pushed with "What backs it:
options: calls flow, $2.1M premium".

The other way round works too:
- **An ignition with nothing behind it** has its chain read at once.
- **Options bought on the same side** in that read, or earlier the same day (within 3 hours),
  confirm it and push it.

The research behind the pairing: Easley, O'Hara and Srinivas (1998) and Pan and Poteshman (2006)
found that option volume leads the stock.

## Company-wide surges

The per-contract rule above misses the most common "unusual options activity": a whole chain
lighting up while each contract stays near its own open interest. On Friday Oct 9 2026, CCI's
$77.5 and $75 calls traded only 1.7× and 0.2× their open interest, yet the chain was on fire.

**🌊 Surge:** the company's total option volume today against the previous session.
- **Shows:** 4× or more, with at least 2,000 contracts.
- **Pushes:** 6× or more, with $1M+ traded across the company.
- **Says:** the multiple, the dollars, the call share, and the top contracts with their moves.
- **Baseline:** Alpaca's previous daily bars when Alpaca is the feed, else what this server saw
  in the previous session.

## Pushes on a big day

- **Spikes:** a company pushes once a day for a spike, and again only if it goes 10× further
  (1,000% → 10,000%). A single name climbing through every step can't spend the day's budget.
- **One per scan:** if one scan finds a spike, unusual volume and a surge on the same company,
  only one of them pushes. The others show in the app.
- **Never traded before:** a contract with no previous close is measured from its model value
  at yesterday's close (at least $0.01), and the alert says so. Brokers measure these from a
  ~$0.01 closing mark, which is why their "% today" can read +12,400%.
- **Overnight news:** a company named in news after the close stays on the fast-read list until
  two hours into the next session. Its morning options and radar alerts cite that news,
  instead of "No headline yet".

## Coverage and speed

- **Which companies:** every common stock in the universe (Nasdaq's screener, the same one the
  small-cap lane uses) worth `min_market_cap` ($1B) or more, about 3,000. A company with no
  listed options is skipped for a day.
- **Hot names, re-read every 3 minutes (up to 40 at a time):**
  - companies moving 3% or more today (on a live price, see "How live it is");
  - names on the price radar's board;
  - any company in a news or price alert, for 2 hours.
- **Everyone else** rotates through, largest first. At ~3 chain reads a second, a full pass takes
  about 15 to 20 minutes.
- **When:** the regular session only. Option quotes don't move outside it.
- **Push limit:** at most 12 options pushes a day (`daily_pushes`). The rest are in the app.

## Blowups: many contracts at once

**The cases (Oct 12 2026):**
- **LITE** fell 9.3% ($1,103.36 Friday to about $1,000) and its Oct 16 puts went 4-13x: the $925
  put went $0.55 → $7.21, the $927.5 put $1.98 → $9.90 during the day. Our one alert said
  "$925 put +36% (unconfirmed)".
- **FLEX** sat at -1.9% while its whole November chain was repriced from about 45% to about 90%
  implied vol. Calls worth a cent were bid $0.20-$0.65, and a broker showed +4,400% to +8,400%.
  Nothing alerted.

**Why they were missed (fixed):**
1. **A stale stock price.** The universe (Nasdaq's screener) holds the *last close*, not today's
   price: it showed LITE at Friday's $1,103.36 (+5.22%) mid-session. So "yesterday's" price came
   out as Thursday's. Every chain read now uses a live stock price (Alpaca, else Yahoo, fetched on
   demand). Without one, the universe's figure is used for what it is, yesterday's close, and
   today's move is left unknown.
2. **Yesterday valued at today's volatility.** The stale-close check valued yesterday's contract
   at today's implied vol, which a crash or an event inflates. It now backs **yesterday's** IV out
   of yesterday's own closes (the median of contracts near the money that closed at $0.20+).
3. **Only a tiny close is a stale print.** A close of `stale_max_prev` ($0.10) or less, far below
   yesterday's value, is replaced by that value. A $0.55 close is a price. A close from *before*
   yesterday (no trade yesterday) is replaced by the contract's value at yesterday's close.

**The blowup alert (🧨).** It counts the contracts on one side (calls or puts), up to 60 days out,
that are worth 4x+ their honest base (`blowup_multiple`) and $0.20+ now. "Now" is the live bid,
else a print today, else the delayed bid.
- **Shows** from 4 contracts.
- **Pushes** at 8+ contracts, when the best is 10x+, or when implied vol is 1.5x yesterday's with
  the stock moving under 3%. That last one is "repriced", and it means the market is pricing an
  event that hasn't reached the stock.
- **Repeats** when the count or the best multiple doubles.
- **Goes first:** it carries the company's push, and the single-contract spike shows in the app.

Each row gives the base, the price now, the multiple and, where the reported close differs, the
"% on a broker" that the reported close implies. Settings: `blowup*` and `stale_max_prev`.

## Misprints

**The case behind it (Oct 12 2026, OPRA prints).** BIIB's $252.5 call (Oct 16) had never traded
before that day, so a broker showed it at a $0.01 placeholder ("+0.00% today"), and its chart
flicked as the quotes came and went. Meanwhile it printed:
- $0.65 at the open;
- one contract at **$0.20** at 10:45 ET;
- 200 at $0.76-$0.85 at 11:05.

All of it on a $0.15 / $1.25 market. In a chain that thin, a resting low bid catches someone's
market sell (the $0.20 print), and a stub offer can sit far below value for a few seconds.

The tape now looks for six kinds of misprint. Every kind except prints is judged on **live
quotes** (at most `live_quote_s` old) and, where the stock price matters, a live stock price. So
each one is something you could act on when it is flagged:

| Kind | What it means | How it's found | Pushed? |
|---|---|---|---|
| **Strike-order arbitrage** | A higher-strike call bid above a lower-strike call's ask, or the mirror for puts: buy one, sell the other, profit locked in | Model-free: a lower-strike call is always worth at least a higher-strike one | Yes, from $0.10 a share |
| **Calendar arbitrage** | A later expiry offered below an earlier expiry's bid, same strike | Model-free: a later American option is always worth at least an earlier one | Yes, from $0.10 |
| **Below intrinsic** | Offered below what exercising it is worth at the live stock price | Model-free | Yes, from $0.10 |
| **Offered far below value** | Ask at most half the fair value and $0.15+ below it, on a contract worth $0.20+ | Fair value: Black-Scholes at the live stock price with the **implied vol of its neighboring strikes** (their tight live markets, interpolated; the contract's own quote is never used) | Yes, from $0.20 below value |
| **Bid far above value** | Bid at 2x+ the fair value and $0.15+ above it: sell to it | Same fair value | No (in the app) |
| **Traded far below value** | A print in the last hour at 40% of the fair value or less, $0.15+ below it, unless the contract's own tight market agrees | Same fair value | No: a thin chain where resting orders fill. A print can also be one leg of a spread |

**No neighbors, no value claim.** The company-wide 30-day IV misses the skew and a day's
repricing, so with no tight neighboring markets there is no value misprint. Arbitrage still
counts: it needs no model. Prints far *above* value aren't called misprints either: on a news
day that is informed buying, and the spike alerts cover it.

**On the stream.** A stub offer can vanish in seconds, so the websocket tripwire also watches each
contract's ask against its fair value (worked out at the last chain read). An ask at half the
value or less, or a bid at twice or more, is judged against the cached live chain at once,
with no REST round-trip.

**Noise control:**
- each contract and kind is shown once a day, and again only when the edge doubles;
- at most `misprint_daily_pushes` (6) pushes a day, a budget separate from spikes and flow;
- one push per company per scan.

**Caveat on the free feed:** Alpaca's indicative quotes are live but adjusted, not the exact
exchange bid/ask, and every misprint alert says so. Confirm the quote on your broker: real
misprints last seconds.

Settings: `misprints`, `misprint_max_dte`, `misprint_min_edge`, `misprint_ratio`,
`misprint_value_edge`, `misprint_min_fair`, `misprint_rich_ratio`, `misprint_print_ratio`,
`misprint_print_minutes`, `misprint_push_edge`, `misprint_daily_pushes` (`news247/config.py`).

## How live it is

**The miss that shaped this (Oct 12 2026).** BIIB gapped up at the open and its $247.5 calls
(Oct 16) went from $0.06 to $0.80 within minutes. The alert arrived after the run, showing
Friday's stock price ($225.71, +3.33%). Two causes:
- **Late trades:** on the free Alpaca feed, option *trades* print about 15 minutes late, and the
  spike test was driven by the last trade.
- **A stale stock price:** the chain's price came from the Nasdaq-screener universe, which still
  showed Friday after the open. So the scheduler didn't know BIIB was moving, and the model value
  of yesterday's close was computed from the wrong day.

**What runs now:**

| Piece | What it does | Delay |
|---|---|---|
| **Quote-led spikes** | A contract whose *live bid* (quote at most `live_quote_s`, 120 s, old) is already 1,000%+ above its close fires, even before its trades print. The bid is what it could be sold at now, so it is bid-confirmed by construction, and the stale-base model check still applies. | the quote's age, seconds |
| **Live stock prices** (`market/live.py`) | Each batch's stock prices come from Alpaca's real-time snapshots (free plan: IEX), and the radar shares the Yahoo prices it reads for the ~600 largest companies every minute. The chain's price, its move and the model base come from these. A universe price older than 15 minutes is never used for today's move. | seconds to a minute |
| **The stock sweep** | With Alpaca keys, every covered company's live price every 60 s (`stock_sweep_s`, 100 a request, about 30 requests). Names that newly start moving 3%+ go straight to the urgent lane, the 20 biggest moves first. | ≤ 1 min to notice, on any $1B+ name |
| **Jumps** | A stock moving 0.7% in 10 minutes ($200B+), 1.2% ($20B+) or 2% (the rest) between sweeps has its chain read at once ("After the alert" → "Earlier: jumps"). | seconds after the price |
| **Live hot list** | Companies moving 3%+ on a *live* price are re-read first, not after the 10-minute universe refresh. | ≤ 1 min to notice |
| **The urgent lane** | A company in a news or radar alert, or tripped by the stream, is read at once, ahead of the rotation (two at a time, at most every 20 s per name). | seconds |
| **The tripwire** (`OptionStream`) | Alpaca's options websocket watches the quotes of up to 190 contracts: the cheap ($0.03-$2), near-dated (≤ 21 days), near-the-money contracts of companies in the news, on the radar, moving 3%+ or just alerted, with contracts already running ranked first. A bid crossing 4x, 6x, 11x, 21x or 51x its previous close re-reads that company's chain at once, and the usual tests decide the alert. | seconds |

**Every options alert now states its data's age:** "Data: quote 4s old, last trade 15 min old",
plus where the stock price came from and when.

**What is still not live, and why:**
- **Cboe only (no Alpaca keys):** quotes and trades are both about 15 minutes delayed. Quote-led
  spikes and the tripwire need Alpaca keys.
- **The free Alpaca feed:** quotes are "indicative": live, but adjusted, not the exact exchange
  bid/ask. Trades stay 15 minutes late, so volume and dollar figures lag. That is why a quote-led
  spike is judged on the bid and says "N contracts so far".
- **Fully real time:** Alpaca's Algo Trader Plus ($99/month) gives OPRA trades and quotes and
  1,000 stream quotes. Set `ALPACA_OPTIONS_FEED=opra`, `options.stream_max_quotes: 1000` and a
  lower `alpaca_request_interval_s`.

**Check it from the server:**

```bash
news247 options --provider alpaca BIIB                # prints "freshness: quotes Ns old, last trades N min old"
news247 options --provider alpaca BIIB --stream 30    # watches its contracts on the websocket for 30 s
```

`GET /api/status` → `options.live` shows the stream (connected, contracts watched, quotes, median
quote lag, triggers), urgent reads, and how many stock prices are fresh, by source.

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

## After the alert: still running, fading, or stopped

**The question that shaped this (Oct 12 2026):** "How am I supposed to know if it keeps going
higher or stops out, assuming I entered late?" The META $785 call (Oct 14) alert went out at
12:51 ET. The contract's real day (OPRA 5-minute bars):

| ET | The $785 call | META |
|---|---|---|
| Fri close | $0.18 | $718.67 |
| 9:30-9:45 | $0.23 → $1.27 (first run) | gap to $748 |
| 9:55-10:40 | back to $0.62-$0.75 | holding $739-$746 |
| 10:45 | $1.40 (second run) | $749.59 |
| 11:05-11:20 | $1.43 → **$1.83, the day's high** (third run) | $751.58, the day's high |
| 11:25-12:35 | $0.98-$1.36 | $743-$748 |
| 12:40 | $1.07 → $1.30 | $745-$748 |
| 12:51 | **the alert** | |
| 12:58 | bid $1.52 / ask $1.69 | $746.48 |

The alert said none of that, and nothing came after it. Now three things do.

### 1. Where you are in the move (on the alert)

With Alpaca keys, the alert reads the lead contract's 5-minute bars for the day (trades: on the
free feed they reach about 15 minutes back):
- when it began to run (the first bar at 2x yesterday's close);
- its high and when;
- how many legs so far. A leg ends when a bar opens or closes 35% under the leg's high.

The META call at 12:58: "push #3 of the day: late in the move; within 8% of the day's $1.83 high:
the move is live, but you pay near the top."

### 2. If you buy now (on the alert)

Every spike and blowup alert ends with the card for the price a late buyer pays: the ask.
For the META call at 12:58 (the real quote, META $746.48):

```
▶ If you buy now at the $1.69 ask (9.4x yesterday's $0.18):
• Its day: began running 09:30 ET · high $1.83 at 11:20 ET · low $0.23 · 1,601 traded (trades through 12:40 ET)
• Where you are: push #3 of the day: late in the move; within 8% of the day's $1.83 high: the move is live, but you pay near the top.
• Each 1% META moves your way: about $1.03 (61% of the entry) · delta 0.11 · implied vol 53%
• Breakeven at expiry: META $786.69 (+5.4% from here)
• If META 1% higher now: $2.63 (+56%) · META flat into the close: $1.44 (-15%) · META gives back half today's move: $0.56 (-67%)
• Exits: fading under a $1.28 bid, stop under $1.04 (20% / 35% under the mid; both trail the best bid) · on the stock: half its move gone at $735.12, all of it at $718.67
• Followed until the close: a follow-up when it runs (+25% steps), fades or hits the stop.
```

How each line is worked out:
- The scenarios are Black-Scholes at the contract's own implied vol (from its mid).
- "Flat into the close" is the rest of today's time decay.
- When the chain's vol is 15%+ above yesterday's, a vol-crush case is added: "implied vol back to
  yesterday's X%".
- The stock lines use the day's high from Alpaca's daily bar, or the prices seen.

### 3. The follow-through (after the alert)

The lead contract of every spike and blowup alert (HIGH or MEDIUM) is followed until the close:
- on the websocket, when there are Alpaca keys: every quote, ranked first on the watch;
- otherwise by re-reading its chain whenever it has had no quote for `follow_read_s` (45 s).

It is judged on the **bid** (what you can sell for) against:
- **the entry:** the ask at the alert, what a late buyer paid;
- **the reference:** the alert's mid, or the best bid since, whichever is higher. Lines hang from
  this, so the spread alone never trips them.

| State | Rule (default) | Follow-up |
|---|---|---|
| **Still running** | a new high, `follow_run_step` (+25%) above the entry, then +25% above each push | 📈 MEDIUM |
| Holding | under the high, above the fade line | none |
| **Fading** | the bid `follow_fade` (20%) under the reference, or the stock has given back half its move today | ⚠️ HIGH |
| **Stop hit** | the bid `follow_stop` (35%) under the reference, or the stock back to yesterday's close (its whole move erased) | 🛑 HIGH, always sent |
| **Running again** | after a stop: a bid above the high it had before the stop, with the stock's move intact; the lines start over from that price | 🔁 MEDIUM |

Guards against noise:
- A drop must show on **two quotes 20 s apart** (`follow_confirm_s`). A market maker pulling a
  bid for a second is not a stop, and if the second quote is milder, the milder state is what
  counts.
- Highs, and "still running", count only when **two quotes in a row** reach them.
- At most `follow_max_pushes` (6) follow-ups a contract, 2 minutes apart (a stop excepted), and
  `follow_daily_pushes` (30) a day.

Every follow-up says:
- **why** it was sent;
- the entry, the best bid since and when, and the bid and ask now with the quote's time;
- **the stock:** "holding near its best level today: the option is losing value faster than the
  stock (implied vol coming out, or time)", or "has given back 60% of its move today". The first
  is a vol or time problem; the second is the move failing;
- the lines now: fading under, stop under, and the next "still running".

**Replay: a META alert at 9:44 ET** (after the 9:40 bar; the real bars, closes as the mid with a
$0.06 spread, ask $1.06):

| ET | Follow-up | Bid |
|---|---|---|
| 9:59 | 🛑 stop hit: 39% under the $1.03 mid, **with META holding $742** (the opening premium draining) | $0.63 |
| 10:49 | 🔁 running again: above the $1.00 it reached before the stop | $1.18 |
| 10:54 | ⚠️ fading | $0.96 |
| 11:19 | 📈 still running: a new high | $1.64 |
| 11:24 | ⚠️ fading: 29% under the $1.64 high | $1.16 |
| 11:44 | 🛑 stop hit: 38% under the high | $1.02 |

An alert at 11:09 (ask $1.41, near the top) would have run to a $1.64 bid (+16%, short of a +25%
step), then gotten ⚠️ fading at 11:24 and 🛑 at 11:44 ($1.02).

That is the honest picture of cheap, short-dated options:
- a mechanical stop is hit by vol draining out while the stock holds (9:59), and the move can run
  again;
- buying the third push near the high tends to stop out.

The follow-ups show both, and say which one is happening. The system never trades: these are the
same rules every time, so the exit is decided before the entry.

**In the app:** open an options alert, and **After the alert** shows the contract's state now:
- the entry, the bid now and the % since the alert;
- the best bid since;
- the fade and stop lines, the next "still running", and the stock.

`GET /api/status` → `options.follow` lists every followed contract and the recent follow-ups.

### Earlier: jumps

Between sweeps, each company's last 30 minutes of live prices are kept. A stock that jumps
reads its chain at once:
- 0.7% in 10 minutes for $200B+ companies (`jump_pct_mega`);
- 1.2% for $20B+ (`jump_pct_large`);
- 2% for the rest (`jump_pct`).

That is a read, not an alert: the usual tests decide.

On the real META tape, the 9:30 → 9:35 closes ($734.08 → $741.94, +1.1%) trip it in the second
minute it shows. The stock sweep now runs every 60 s (it was 120 s).

Settings: `follow`, `follow_per_alert`, `follow_fade`, `follow_stop`, `follow_run_step`,
`follow_confirm_s`, `follow_read_s`, `follow_hours`, `follow_max_pushes`, `follow_gap_s`,
`follow_daily_pushes`, `follow_max`, `follow_bars`, `jump_window_s`, `jump_pct_mega`,
`jump_pct_large`, `jump_pct`, `jump_cooldown_s` (`news247/config.py`). Followed contracts survive a
restart within the day (`options_follow.json` next to the radar's state).

## Day moves

When a $50B+ stock is having its directional day, the tape calls it at the 9:45 hold. It then
names the options to buy, with exact prices, a pullback zone and the line that ends the trade.
See [DAYMOVE.md](DAYMOVE.md), and the study it comes from in
[research/options-volume-strategy-2026-10-12.md](research/options-volume-strategy-2026-10-12.md).

## Where to see it

- **Your phone:** HIGH alerts push like any other alert. The app shows every options alert under
  the source label **OPTIONS**.
- **`GET /api/options`:** the latest hit per company, plus the feed's health: companies covered,
  chains read today, rotation position, stale chains, errors and back-off.
- **`GET /api/status`:** the same health, under `options`.

## Files

- `news247/market/options.py`: the parser, Black-Scholes, the spike and volume rules, and the
  feed.
- `news247/market/daymove.py`: day moves: the call, the picks, the volume tape, the desk
  ([DAYMOVE.md](DAYMOVE.md)).
- `news247/market/follow.py`: after the alert: the contract's day from its bars, the "if you buy
  now" card, and the follow-through desk (running, fading, stopped, running again).
- `news247/market/live.py`: live stock prices, each stock's day path and the jump detector, and
  the websocket tripwire.
- `news247/engine.py`: alert text and wiring (`on_options`, `_options_alert`, `_play_lines`,
  `on_follow`, `_follow_alert`, `options_board`).
- `news247/config.py`: `OptionsConfig`, every threshold with a comment.
- `tests/test_options.py`: 55 tests, covering pricing, parsing, every rule, the feed (pacing,
  priorities, back-off, freshness, problem reports), the engine, the API, the CLI and the NWE
  replay.
- `tests/test_options_follow.py`: 25 tests, covering the day path and jumps, the option's bars and
  legs, the play card, every follow rule, the real META replays (9:44 and 11:09 alerts), the feed
  wiring (websocket, follow lane, jump reads), the engine text and the app's state.
