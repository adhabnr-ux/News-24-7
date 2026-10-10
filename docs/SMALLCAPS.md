# The little things: small caps that move big

Bobby Axelrod's edge was never just the mega caps everyone watches. It was the $200M company
whose FDA letter, buyout or Army contract he knew about first. That company then moved 60%,
150% or 300% while the rest of the market was still reading about Nvidia.

News247 covers both:

- **The big stocks** (everything before this page): AI labs, the Fed, tariffs, megacap
  earnings and sector baskets.
- **The little things** (this page): every listed US company is sized against its own news, and
  a radar watches the whole small-cap market for names breaking out before any headline.

## 1. Know every company's size

`news247/market/universe.py` holds about 7,000 Nasdaq, NYSE and NYSE American listings:
symbol, name, market cap, price, volume, sector and industry.

- **Where it comes from:** Nasdaq's free public stock screener (the JSON behind
  nasdaq.com/market-activity/stocks/screener). One request returns the whole market.
- **Caching:** it is saved to `data/universe.json.gz`, so a restart, or a day the endpoint is
  down, still knows every company.
- **Refresh:** every 6 hours, and every 10 minutes during market hours.

Load it by hand, or look companies up:

```text
$ news247 universe --refresh             # download now; prints listings per size band
                                         # (mega/large/mid/small/micro/nano) and how many
                                         # companies are in the small-cap lane
$ news247 universe CAPR "Serve Robotics" # look companies up by symbol or name:
                                         # market cap, band, price, sector, pump-risk flag
```

### Recognizing the company in a headline

The company behind a headline is identified four ways, from strongest to weakest:

1. **Wire tags.** GlobeNewswire and PR Newswire attach `Nasdaq:ACMB` to each release.
2. **Tickers in the text:** `(NASDAQ: ACMB)`, `$ACMB` or `(SERV)`.
3. **The name the headline starts with**, as in "Soleno Therapeutics Announces…".
4. **A name anywhere in the headline**, as in "Nvidia discloses stake in Serve Robotics" or "FDA
   panel votes against Capricor's…". This needs either:
   - the full name, or
   - a distinctive first word that only one company owns, is 6 or more letters, and isn't
     ordinary vocabulary (so "Capricor" counts and "Global" never does).

## 2. Size the news against the company

`news247/analysis/smallcap.py` reads a headline the way a small-cap trader does. It finds the
catalyst, estimates the typical move **for a company that size**, and turns that into score
points plus a floor.

| Catalyst | How it's sized | Typical move used |
|---|---|---|
| Being acquired | the premium: stated ("62% premium"), or the offer per share vs. the last price | ≈ the premium |
| FDA approval / CRL / panel vote / "FDA agreed" | binary for a one-drug biotech; scaled down for larger and non-biotech companies | ±25–45% for a small biotech |
| Trial readout | positive, negative, or "Topline Results" with no verdict (flagged as a binary event) | +35% / −55% / ±40% |
| Contract, order, lease | dollar amount ÷ market cap. Ceilings ("up to", IDIQ) are discounted; government buyers get a premium | 5% of the company ≈ +7%, 25% ≈ +25%, 100% ≈ +70% |
| NVIDIA / OpenAI / hyperscaler / big-pharma deal or stake | name and size. "NVIDIA Inception", "available on AWS Marketplace" and clinical-supply deals don't count | +30–40% for a micro cap, more if the deal is large vs. the company |
| US government equity stake | DoD, DOE, Office of Strategic Capital, "strategic investment by the federal government" | +36–45% |
| Crypto-treasury PIPE | deal size ÷ market cap. Labeled as fading often | up to +80% |
| Offering / registered direct | size ÷ market cap | −10% to −40% |
| Short-seller report | known firms (Hindenburg, Muddy Waters, Culper…) or "we are short" | −20% to −30% |
| Bankruptcy, delisting, going concern, restatement, default, reverse split | fixed | −10% to −50% |
| Index inclusion, guidance raise or cut, uplisting | fixed | ±8–25% |
| A T1/T12 trading halt on a small cap | news about to drop | ±30% |
| Readout or PDUFA date announced | "arm a watch" | shown, not pushed |

How the expected move becomes a score:

- **≥ 25% → at least HIGH**, pushed to your phone.
- **15–25% → at least MEDIUM**, shown in the app.

So a $70M drone maker's Army contract gets pushed even if no keyword in its release fires:

```text
$ news247 score --tier wire 'Dronez Systems (NASDAQ: DRNZ) Awarded $45 Million U.S. Army Contract'

  🟠 HIGH  score 70.0/100   direction: up
  tickers:  DRNZ
  small cap: DRNZ $70M micro cap · Contract from U.S. Army worth 64% of market cap · +36–96% typical

    +12 wire source
    +32 DRNZ $70M micro cap: Contract from U.S. Army worth 64% of market cap, +36–96% typical
    +26 small-cap floor (a 60% mover)
```

The same headline about an untracked company with no market cap known stays quiet. That has
always been the rule for tickers nobody tracks.

### Pump-and-dump guards

A catalyst is shown but **never boosted** when:

- the company is under **$30M** market cap. Exception: a deal bigger than the whole company,
  such as a $425M PIPE into a $10M shell, which is a real capital event.
- the stock is under **$1**.
- it is a recent **China/Hong Kong micro-cap IPO**. Nasdaq says about 70% of its manipulation
  referrals come from this profile.

Routine small-cap traffic is penalized:
- minimum-bid notices and "regains compliance"
- conference presentations
- inducement grants
- letters of intent and MOUs
- "joins the NVIDIA Inception program"

Large caps are never touched: above `max_market_cap` (default $10B) the big-cap scorer is in
charge.

## 3. The radar: moving before the news, at every size

Small-cap news often reaches the tape before any feed:
- an 8-K accepted at 16:01
- a Reuters scoop on a government stake
- a deal a few desks heard about first

The price is the first public trace. `news247/market/radar.py`:

- **Scans every minute from 04:00 to 20:00 ET on trading days.**
  - Every minute: Yahoo's small-cap gainers (with pre-market and after-hours prices) and, in the
    regular session, its all-size day gainers and losers.
  - Every minute, pre-market and after hours included: a sweep of the **400 largest companies**.
    Yahoo's screeners only rank the regular session, and this is what caught MRNA's 9% gap up
    before the open on Oct 9 2026.
  - Every 10 minutes in session: the full Nasdaq screener snapshot.
- **Thresholds scale with size.** A 20% day is routine for a micro cap and a once-a-year event
  for a $75B company:

  | Size | Flags at (day / fast jump) | Pushes at |
  |---|---|---|
  | under $2B | ±20% / ±8% | ±35% with $5M+ traded or 5× volume |
  | $2–10B | ±10% / ±5% | ±15% |
  | $10–200B | ±6% / ±3% | ±9% |
  | $200B+ | ±4% / ±2% | ±6% |

  A stock flags again at each higher rung (1×, 1.75×, 2.5×, 3.75×… the threshold), never twice
  at the same one. Pushes are capped at 8 a day for small caps and 8 for larger ones. The rest
  show in the app.
- **Small caps must have real trading.** Under $2B, at least $2M must have traded and the price
  must be $1 or more. Relative volume vs. the 3-month average is shown. From $2B up a company is
  liquid by definition.
- **Survives restarts.** What already fired today is saved to `data/radar_state.json`. A restart
  doesn't repeat an alert, and a stock that was already moving when the server started is still
  flagged. There is no silent start-up baseline.
- **Cites the calendar.** When the stock is on today's calendar, the alert says *"On the calendar
  today: MRNA joins the Nasdaq-100 (effective at the open)"* instead of "No headline yet".
- **Links the headline if one exists.** If not, the alert says so: *"No headline yet — on the
  radar before the news."* When the headline then lands, its alert says *"📡 Radar flagged ACMB
  +45% 12m before this headline."*

## 4. Knowing earlier than everyone

The radar and the catalyst desk react within minutes. Two more edges get you there before the
news itself.

### The date, before the move

Companies announce *when* the coin will be flipped, weeks ahead:
- "Kodiak Sciences to Present Topline Results on September 28, 2026 from DAYBREAK Pivotal
  Phase 3 Study…" was published Sep 25. KOD went +178% on the 28th.
- "…PDUFA target action date of June 30, 2027"
- "FDA Advisory Committee Meeting Scheduled for July 29 to Review…"
- "Moderna and Merck to Present Phase 3 … Data at ESMO 2026 Presidential Symposium" (Oct 24)
- **Index entries:** "Nasdaq Announces Moderna to Join Nasdaq-100 Index … prior to market open on
  Friday, October 9". This becomes "MRNA joins the Nasdaq-100 (effective at the open)" at 09:30.
  Index funds must buy, and MRNA was +9% before the open.

These are captured for companies of **any size**. The subject is found from the wire's ticker
tag, "(Nasdaq: MRNA)" or the company name. The index provider (Nasdaq, S&P) is never mistaken for
the subject.

`news247/analysis/catalyst_dates.py` reads these headlines, and their summaries where the date
often sits. It finds the readout, PDUFA or panel date and the time ("8:30 a.m. ET"), and puts
the event on the **Calendar** as a gold **BINARY** entry. The entry shows the company, its size
and a link to the release. Captured events are stored, so a restart keeps them.

The **morning Brief push** says *"Tomorrow 08:30 ET: KOD · Phase 3 trial readout"* the
morning before, when you position. It says *"Today …"* on the day.

### The giants' new stakes, the hour they're filed

On Feb 14 2024 NVIDIA's first 13F holdings filing listed 1.73M SoundHound shares, and SOUN
closed +67% the next day. A year later NVIDIA's 13F showed it had sold out, and SOUN fell 28%.

A 13F's title says nothing about what's inside, so the `sec-13f-giants` source reads the
holdings table itself:
- **Who it watches:** NVIDIA, Alphabet, Amazon and Berkshire Hathaway by default. Add any CIK
  under `filers:`.
- **What it reads:** each new 13F-HR, compared with the previous quarter's.
- **What it reports:** one alert per **new stake**, **stake raised 2×+** or **exit**, smallest
  companies first. For example: *"NVIDIA 13F: new stake in SoundHound AI, Inc. (SOUN) — 1.73M
  shares, $3.7M"*.
- **How it's sized:** the small-cap desk treats a giant's new stake as a re-rating (bullish)
  and an exit as the reverse (bearish).
- **When it polls:** every 10 minutes, and every 2 minutes in the days around the 13F
  deadlines (about Feb 14, May 15, Aug 14 and Nov 14).

## 5. In Foretape

- **Cards:** news about a small or mid cap carries a gold **MICRO CAP / SMALL CAP / MID CAP** pill
  with the symbol, market cap, catalyst and typical move. Radar alerts carry a **◉ Radar** pill
  with relative volume.
- **Tape:** an **All / Small caps** switch filters the tape. The choice is remembered on the phone.
- **Watch tab:** the **Small-cap radar** board lists names ripping right now, with size, dollars
  traded, relative volume and the headline behind the move if there is one.
- **Detail sheet:** a "The little thing" / "On the radar" section shows the company, size,
  catalyst, expected move, deal size vs. the company, and the linked headline.
- **Lock screen:** the push body leads with
  `DRNZ · $70M micro cap · Contract from U.S. Army worth 64% of market cap · +36–96% typical`.

## 6. Sources added for small caps

These catalyst feeds are on by default:
- GlobeNewswire: Mergers and Acquisitions, Business Contracts, Clinical Study, Financing
  Agreements, Biotechnology, Defense
- PR Newswire: FDA approvals, aerospace and defense
- Business Wire: aerospace
- Newsfile: biotech, mining
- the FDA drugs feed

They sit alongside the existing firehoses, SEC 8-K/13D/13G and Nasdaq halts. The firehoses
carry about 20 items each, which is only minutes of releases on a busy morning; the
topic feeds make sure no catalyst slips past.

## 7. Configuration

```yaml
smallcap:
  enabled: true
  min_market_cap: 30000000      # pump floor (SMALLCAP_MIN_CAP)
  min_price: 1                  # SMALLCAP_MIN_PRICE
  max_market_cap: 10000000000   # above: big-cap scorer
  refresh_hours: 6
  radar: true                   # SMALLCAP_RADAR
  radar_provider: yahoo         # or nasdaq (snapshot only)
  radar_seconds: 60
  radar_max_cap: 2000000000
  radar_min_pct: 20
  radar_jump_pct: 8
  radar_min_dollar_volume: 2000000
  radar_push_pct: 35            # SMALLCAP_RADAR_PUSH_PCT
  radar_daily_pushes: 8         # per class (small / big); SMALLCAP_RADAR_DAILY_PUSHES
  radar_mid_min_pct: 10         # $2-10B   (jump = half, push = 1.5x)
  radar_large_min_pct: 6        # $10-200B
  radar_mega_min_pct: 4         # $200B+
  sweep_size: 400               # largest companies swept for pre/after-hours moves
  sweep_seconds: 60
```

API:
- `GET /api/radar?limit=40` returns the radar board, market phase and feed health.
- `/api/status` gains a `smallcap` block with listing count, universe age and radar scans/hits.

## 8. Does it work? The backtest

`news247/data/history.yaml` now includes 33 small-cap events from 2024 to Oct 2026. Examples:
- Navitas +164% on "NVIDIA Selects Navitas…"
- Trilogy Metals +211% on the government stake
- Spero +244% on "Stopped Early for Efficacy"
- uniQure, Capricor and Kodiak readouts
- the 2024–26 CRLs
- Roche/Poseida, Sanofi/Vigil and Lilly's buyouts
- the 2025 crypto-treasury PIPEs

It also includes 16 lines of typical small-cap press-release noise. Each event carries the
market cap the universe would have known that morning.

```text
$ news247 backtest
  Caught from the FIRST report:    178/186  = 96%
  False alarms on noise:           0/122  = 0%
    ✓ smallcap_acquired 6/6   ✓ smallcap_trial 6/6   ✓ smallcap_gov_stake 4/4
    ✓ smallcap_crypto_treasury 4/4   ✓ smallcap_fda 6/6   ✓ smallcap_mega_stake 3/3 …
    ✗ smallcap_dilution 0/1   (a 12%-of-market-cap offering: shown in the app, not pushed)
```

The research behind it (events, venues, moves, patterns, feeds) is in
[docs/research/small-caps.md](research/small-caps.md).

## Honest limits

- **Typical moves are rules of thumb, not forecasts.** Small caps also fade:
  - a positive readout is often followed by an offering the same afternoon
  - crypto-treasury PIPEs reversed within weeks
  - government-stake scoops reversed on denial

  The ranges are deliberately wide.
- **The Nasdaq screener is a snapshot, not a tick feed.** Use it for size; the radar's
  minute-by-minute timing comes from Yahoo.
- **These endpoints are unofficial.** Both Nasdaq's screener and Yahoo's screener answer
  browsers best. The feed sends browser headers and performs Yahoo's cookie/crumb exchange, and
  it keeps the cached universe when either is unreachable. `news247 universe --refresh` shows
  whether your host can reach Nasdaq.
- **Not covered:**
  - ACCESS Newswire, which has no public release feed
  - OTC stocks, which are deliberately excluded
