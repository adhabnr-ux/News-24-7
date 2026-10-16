# Foretape: News247 on your home screen, with instant push alerts

**Foretape** ("before the tape moves") is News247's own phone app, built for the way a trading desk reads news: what happened, which way it cuts, what happened last time, and how far ahead of everyone else you are. It's a website you add to your home screen, so it looks and behaves like an app:

- **Push notifications on your lock screen**, sent the moment News247 decides a story matters.
- The same alert also appears in the open app instantly, over a live stream.
- **No app store, no phone number, no Meta/WhatsApp account, no fees.**

It works on iPhone and iPad (iOS/iPadOS 16.4 or newer), Android, and desktop Chrome, Edge, Firefox and Safari.

```
News247 server ──(encrypted Web Push)──▶ Apple / Google push service ──▶ your phone's lock screen
      └──────────(live stream)──────────────────────────────────────────▶ the open Foretape app
```

## Install on iPhone (2 minutes)

1. On your phone, open this link in **Safari**. Your server's address and `DASHBOARD_TOKEN` are under Render → news247 → Environment.
   ```
   https://<your-app>.onrender.com/app/?token=<DASHBOARD_TOKEN>
   ```
   Or open the **Setup page** (`/setup?token=…`) on a computer and scan its Foretape QR code with the iPhone camera.
2. Tap **Share** (the square with the arrow), then **Add to Home Screen**, then **Add**.
3. Open **Foretape** from your home screen and tap **Turn on alerts**. When iOS asks, tap **Allow**.
4. A test notification arrives within a second or two: *"🔴 Foretape test: alerts will arrive like this"*.

That's it. Close the app; alerts still arrive.

> **Why the home screen?** Apple delivers web notifications only to sites added to the home screen, and the permission prompt can only come from a tap. That's why there's a button and not an automatic prompt. The app's first screen walks you through it.

**Android / desktop:** open the same link in Chrome, Edge or Firefox, tap **Turn on alerts**, and allow. Installing it (menu → *Install app* / *Add to Home screen*) is optional but gives it its own icon.

## What you see

Foretape opens on a **live photograph**: a rocket climbing out of its own smoke at golden hour. It is not a video or a gradient. A real launch photo is rendered by a WebGL shader (`scene.js`) that adds what a still can't have:

- **Depth.** Tilt your phone (or move the mouse) and the near smoke moves more than the far sky.
- **Life.** Clouds drift and the smoke billows. The plume flickers, heat shimmers above the pad, embers rise, and the wet ground glints. A new alert makes the plume flare.
- **Altitude.** The camera climbs as you scroll: past the cirrus, off the top of the photo and into a starfield. Each tab sits at its own altitude, so switching tabs flies the camera. An altitude rail on the right edge shows where you are.
- **Mood.** The light is a little brighter while the market is open and dusky while it's closed. A CRITICAL alert in the last 15 minutes warms the edges of the frame.
- **Callouts pinned to the rocket** show live data: how many sources are up, the last alert and who it hit, and your median lead over the news. They are projected through the same camera as the shader, so they stay on the rocket.

Everything on top of the photo is glass, lit like the scene: a cold rim on the top edge, a warm bounce on the bottom, a highlight that slides with the tilt of your phone, and film grain. Cards swing up out of depth as they scroll in and curve on a cylinder as they pass eye level. Swipe a detail sheet down to dismiss it.

**Phones that can't take it:** on WebGL-less browsers the plain photo is shown with CSS parallax; with *Reduce Motion* turned on, a still frame; and a slow GPU lowers the render resolution automatically. Add `?scale=0.5` to the address to render fewer pixels yourself.

**iPhone motion permission.** iOS asks before a web page may read the gyroscope. Foretape shows a *"✦ Tilt your phone: enable motion"* chip once, and the Desk tab has a switch. On Android and desktop there's no prompt (mouse movement drives the tilt on desktop).

Five tabs:

| Tab | What's on it |
|---|---|
| **Tape** | The hero line, then three numbers: your **median lead over the news** (7 days), alerts in the last 24 h, and sources live. Then the **next big catalyst** on the calendar. Below that, **Top of the tape** (the strongest alert of the last 6 h, in large type) and every other alert. Tap any alert for its full dossier. Above the tape: **your watchlist** as a live strip (tap a name for its page, **＋** to add), and **search** across tickers, company names, 30 days of alerts and headlines (type a ticker and press Enter to jump straight to its page). Every card that has been graded shows how it played out on its stock: *"ACME: 1h +1.2% ✓ · close −0.4% ✗ · next day +2.1% ✓ · 5 days +6.0% ✓"* (✓ = it went the alert's way). A card with other alerts on the same stock in the last 24 hours carries *"+2 more on ACME"* (tap for the stock's page). **Newest / Best first** above the tape re-sorts it by severity, the alert's base rate, whether it's on your stocks, confluence and freshness. Come back after 20 minutes or more and a **While you were away** card sits on top: how long you were gone, how many alerts fired (CRITICAL and HIGH counted), the three that matter most, and your stocks that moved 2%+. The **All / Mine / Small caps** switch filters the tape: **Mine** keeps only alerts on stocks you watch (cards on them carry a gold **★ yours**), **Small caps** keeps small- and mid-cap catalysts and radar hits (remembered on the phone). |
| **Brief** | After 16:00 ET, **Tomorrow** (the next session: after-hours movers and why, who reports, with yours starred and ☀/☾ for before/after the bell, and the scheduled events; also pushed at 20:00 ET Sunday to Thursday to phones that turn it on) and **The close**: the indexes, breadth and the regime, the sectors that led and lagged, the biggest movers with their reasons, and how today's alerts did by the close (also pushed at 16:15 ET to phones that turn it on). Then **the day so far**: today's alerts as dots on a 4:00-20:00 ET time axis (CRITICAL and HIGH on top), over the S&P 500's path (SPY, marked every 5 minutes while the server runs); tap a dot for its alert. Then the morning meeting: **your stocks** first (each watched stock's move, the overnight headline behind it, an earnings report or calendar event in the next two days, and how many alerts it drew since the close), then catalysts **since the last close**, the themes in focus, SPY/QQQ/IWM/DIA and the biggest movers, the week's calendar, and **your edge** (stories you had before the mainstream, median and biggest head start). |
| **Calendar** | **Earnings this week** first: every company worth $2B+ reporting in the next 7 days (Nasdaq's earnings calendar), by day, biggest first, marked ☀ before the open or ☾ after the close; tap one for its page (implied move, past reactions). Then scheduled catalysts for the next 60 days, with time (ET) and impact: FOMC decisions, jobs reports, elections, option expirations and quad witching, market holidays and early closes, plus one-offs like the end of China's rare-earth suspension. **Binary events are added automatically** from headlines that announce them (a Phase 3 readout date, a PDUFA date, an FDA panel), marked with a gold BINARY tag and linked to the release; the morning Brief warns you the day before. Edit `news247/data/calendar.yaml` to add your own (earnings dates, investor days). |
| **Watch** | **In play** first: one ranked list built from every board at once (the day's move, volume against normal, headlines this hour, options flow, a confluence alert, the radar, and a known reason), each row saying why, with the **market regime** above it: *▲ Risk-on*, *▼ Risk-off* or *◆ Mixed*, read from cyclical vs defensive sectors, breadth, and small vs large caps, with the numbers shown. Then **Breadth**: how many companies worth $300M+ are up and down on the live tape (a bar), the cap- and equal-weighted move of all of them, how many are up or down 5%+, and the split by size (large, mid, small...): a stock market or a market of stocks. Then the **radar**, led by stocks **igniting** in the last hour (⚡ "+2.1% in 5m on 6× vol": a push on volume a median 20 minutes before they become big movers; tap one for its launch price, where the move fails and what backs it). Then small caps breaking out right now on real volume (±20% on the day or a fast jump, $2M+ traded), with market cap, dollars traded, relative volume, pre-market/after-hours tag and the headline behind the move, or "No headline yet". It is live: it refreshes every 30 s while on screen and instantly when the radar fires (new names flash gold, moves count to their new value), and tapping a flagged name opens its radar alert. Then **Gappers**: every company worth $300M+ moving 3% or more on the whole-market tape (before the open, the pre-market gappers), up and down, each with the headline behind it (overnight news counts) or *No headline found*. Then **Unusual volume**: every stock trading 3× or more its normal volume for this time of day (from the ignition scanner's read of the whole-market tape: volume so far ÷ a normal day's volume by this minute), with its move, dollars traded and the headline behind it, or *No headline found*. Volume often moves before price. Then **Buzz**: stocks the news is suddenly about: 3+ headlines in the last hour from 2+ sources, against their usual hourly count over the past week (*"4 headlines in the last hour · usually almost none · 40×"*). Then **Halts**: the last 24 hours of trading halts on every US exchange from Nasdaq's feed, with the reason, code and resumption time (a halt on a stock you watch reaches your phone even when it scored under the alert line). Then **Smart money**: one feed of informed money as it prints, newest first: options bought hard on one side (◈), insiders buying with their own cash on Form 4 (✦), and stocks igniting on volume (⚡), each with ▲/▼. Then the **Market map**: today's cap-weighted move for every sector (companies worth $300M+ with a live quote, 3+ per sector), green to red by strength, with each sector's leaders and its worst laggard (tap a ticker for its page, or the tile for the sector: each industry's move and every stock in it, best to worst). Then the board: every quoted symbol sorted by today's move, with the 5-minute change. Tickers from fresh alerts are added automatically. |
| **Desk** | **Yours, on this phone** (see [Your desk](#your-desk-per-phone-watchlist-price-alerts-tracked-calls)): your **watchlist** with live prices, your **price alerts**, your **tracked calls** with their P&L and your record, the **track record** of every alert Foretape sent (graded an hour later and at the close), and **what reaches you** (categories, quiet hours, watchlist pushes). Then push status, **Send a test alert**, **Reconnect**, motion on/off, the server's alert level (**Critical only / Normal / More**), **Pause 30m / 2h / until resumed**, **Resume**, the Brief time, and engine health. |

Terms that may be new carry an **ⓘ**: tap it for a plain-English explanation (implied move, call and put walls, max pain, put/call, the evidence tally, base rates, confluence, sympathy moves, relative strength, digests). On a computer: **/** searches, **1-6** switch tabs, **w** watches the open stock, **Esc** closes, **?** lists the keys; wide screens show the tape in two columns.

The next-catalyst card on the tape counts down to a release due today (*"CPI · 8:30 ET · in 14m"*), glows in the last half hour, and says *just out* for 30 minutes after.

A HIGH or CRITICAL alert that arrives while you're on another tab slides up as a glass toast; tap it to open the alert. Pull down at the top of any tab to refresh it: a ring fills as you pull, the flame lights when you've pulled far enough, and the engine flares as the data reloads.

### Every alert comes with an analysis

| Part | What it tells you |
|---|---|
| **The little thing** | For a small or mid cap: the company, its market cap and size band, the catalyst, the typical move **for a company that size** (*"Contract from U.S. Army worth 64% of market cap · +36–96% typical"*) and the deal size as a share of the company. On cards it is the gold **MICRO CAP / SMALL CAP / MID CAP** pill; radar alerts carry **◉ Radar** with relative volume. See [SMALLCAPS.md](SMALLCAPS.md). |
| **The play** | **▲ Bullish / ▼ Bearish / ◆ Two-way**, a 5-bar **conviction** meter (Low → Maximum, from the score), the **direct** tickers and the **read-through** names (suppliers, competitors, the sector basket). When the headline itself doesn't say which way it cuts (a Fed hike, a tariff post, a Polymarket jump), the direction comes from how the most similar past events traded, and the app says so. |
| **Precedents** | The most similar market-moving events on record (150+ since 2016, including every Jun–Oct 2026 mover we traced), matched on the same themes, entities and tickers the scorer found. Each shows what the stocks did then. Example: an FHFA post about VantageScore → *"Last time (Sep 28, 2026): FICO −25 to −27%"*. The top precedent is also on the lock-screen notification. |
| **The tape since** | Each direct ticker's move since the alert ("FICO −9.76% since alert"), live from the price feed. |
| **The edge** | How many seconds after publication Foretape caught it, where it was first seen, and when the mainstream caught up: *"Beat CNBC by 6m 52s"*. It's measured live: when CNBC, Yahoo Finance, Google News, MarketWatch, WSJ, Bloomberg, NYT, Fortune or Axios later carries the same story, the gap is recorded. |
| **The story** | Every headline in the story, first one first: the source, how long after the first, its score, and a link. (With one headline per source: **Who carried it**.) |
| **Earlier alerts like this** | Alerts of the same kind on the same stock in the last 180 days, each with its grade, and how they did by the close as a group. |
| **Why Foretape flagged it** | The full score breakdown: every keyword, theme, authority and source signal that counted. |
| **Was this useful?** | 👍 / 👎, one vote per phone; the counts are every phone's, kept on the alert. |
| **Edge score** | One 0-100 number per alert (also on its card), a weighted composite of the measured facts on its page: how alerts like it did (45%), whether the stock had already moved (20%), how many kinds of evidence agree (15%) and severity (20%). Every part is listed; it summarizes, it doesn't forecast. |
| **Priced in?** | How far the stock had already moved in the 15 minutes before the alert (from the live tape's recent path), and how alerts that came after a move like that did: *fresh* (under 1%), *already moved its way*, or *moved against it first*. The card says it too: *"ACME had already moved +2.5% in the 15 minutes before (its way)"*. |
| **How alerts like this did** | The base rate: every alert is graded (an hour later and at the close, on its own stock, in its own direction), and a new alert carries how its group did over the last 60 days: *"Alerts like this (Options): 62% went their way in 1h, 58% by the close · 31 graded"*. Groups are headlines and macro by severity, then radar, options, insider buys and confluence. Shown only once 8+ alerts in the group are graded; on the card, the sheet and the lock-screen notification. The sheet also draws the **typical path**: the group's median move in the alert's direction at 1 hour, the close, the next day and 5 days. A group that went its way **40% of the time or less over 20+ graded alerts** is flagged **⚠ Historically fades** (and the notification says *historically fades*): the alert may be worth fading, not following. |
| **What lines up** | On a **confluence** alert: each piece of evidence, its side and its weight (see below). |
| **Act on it** | A door to each stock's page, **▲/▼ Track** (a paper call at the live price, kept on your desk with its P&L; the direction follows the play) and **Share**: the phone's share sheet (or a copied link) with a deep link straight to this alert. Whoever opens it needs their own access key. |

### Confluence alerts: when the evidence piles up on one side

The other alerts each see one thing: a headline, options bought, an insider buy, the tape igniting. A confluence alert fires when **independent kinds of evidence line up on the same side of the same stock**: the evidence tally (below) leans clearly one way, with **3 or more kinds** on that side (headlines, options flow, insider buying, the tape), a combined weight of **4+**, and at least one non-insider signal from **the last two hours** (something is happening now, not just an old filing). Weight **7+** is HIGH and pushed (at most 8 a day); under that it's MEDIUM, in the app and pushed to phones watching the stock. Each stock and side fires once a day, only while the market (pre-market through after-hours) is trading. The title says what lined up: *"▲ ACME: insider buying, headlines and options flow line up"*. Like every alert it is graded, so its own base rate builds up.

### Every stock has a page

Tap any ticker chip, anywhere (an alert card, the radar, smart money, the market map, search, your watchlist) and its page slides up. Deep link: `/app/#t=NVDA`.

| Part | What it shows |
|---|---|
| **Header** | The live price and today's move (from the live tape or the price monitor, whichever is fresher), the day's range, market cap, size band, sector, OTC flag. **Session:** above or below VWAP (the tape's volume-weighted average price) and against the 9:35 price (the end of the first five minutes). **Relative strength:** the move against the S&P 500 (SPY) and against its own sector (cap-weighted, live), read as *Its own move*, *Moving with its sector* or *In line*. The **next earnings date** when Nasdaq's calendar has one. |
| **Actions** | **☆ Watch**, **🔔 Price alert** (with one-tap lines: the day's high and low, the call and put walls, and the implied-move bounds) (above $X, below $X, ±N% on the day, or a trailing drop from the high), **▲ Track / ▼ Short** (paper), **Share**, **🔕 Mute 4h** (this phone stops getting alerts that are only about this stock; they stay in the app). Your open price alerts and tracked calls on this stock are listed right there. |
| **Recent tape** | Its intraday price line. |
| **The evidence** | A weighted bull/bear tally of everything Foretape knows about it, with a lean (**▲ leans bullish** only when the bull side is at least twice the bear side and 1.5+ points ahead; likewise bearish; else **mixed**). Counted: alerts from the last 3 days by severity (HIGH = 2, CRITICAL = 3), options bought on one side today (2), insider buys from the last 90 days (opportunistic 3, routine or 10b5-1 planned 0.5, otherwise 2, following Cohen, Malloy and Pomorski 2012), other scored headlines (0.5) and an ignition on the tape (1, or 1.5 when its option chain primed it). Every signal is listed with its weight, and tapping one opens its alert. It is a tally of the evidence, not a forecast. |
| **Why it's moving** | The headlines behind today's move (including overnight news, the reason for a morning gap). When there are none, **Find why now** searches the company's EDGAR filings and the news right then (rate-limited per stock); whatever it finds is scored, stored and alerted like any other item. |
| **Your note** | A note on the stock, for this phone (*"Lockup ends Nov 3"*); watched stocks with a note carry 📝 on the Desk. |
| **On the tape** | An ignition (with its burst volume), the radar's read (dollars traded, relative volume) and the latest options hit (call vs put premium, top contracts). |
| **What options price in** | The at-the-money straddle (call + put at the strike nearest the stock) for the next expiries within 60 days: *"By Fri Oct 23: ±6.1% ($94.20 – $106.40)"*, with 30-day implied vol. An expiry after an earnings date is marked *earnings inside*: that's what the premium is paying for. Read from the live chain when you open the page (cached 10 minutes); mids when both sides are quoted, else last trades (and it says so). From the same read, **positioning**: today's put/call volume (expiries within 45 days), and for the nearest expiry with open interest the **call wall** (the strike above the stock with the most call open interest), the **put wall** (below, puts) and **max pain** (the strike where option holders, in total, would collect least at expiry). Open interest the feed didn't give is left out, never counted as zero. |
| **Peers** | Its industry peers (its sector when the industry has fewer than 3), nearest in size first, with today's moves and their average. When the stock itself moved 3%+, the peers that **haven't moved yet** (a quarter of its move or less, same direction) are named: where a sympathy move would come from, if it comes. |
| **Past earnings** | The last reported quarters from Nasdaq's earnings table: EPS against the estimate, and the stock's two-day move around each report (the close before the report day to the close after it, so before-the-open and after-the-close reports both count). The average move, how many beat, and, next to the implied move above, whether options price the next report bigger or smaller than its usual move (*"±7.5% (0.75× its usual move)"*). Cached 12 hours. |
| **Foretape on it** | Every alert on the stock in the last 30 days, its direction, its move **since the alert** (from the alert's own reference price), and its grade an hour later and at the close. |
| **Insider buys, Coming up, Headlines** | Form 4 purchases (180 days), calendar events naming it, and 7 days of scored headlines. |
| **Company** | Market cap, industry, and how many phones watch it. |

## Your desk: per-phone watchlist, price alerts, tracked calls

Each phone (each browser) gets a random id the first time it opens Foretape. Everything you set is kept on the server under that id, so the server can respect it when it pushes, and it survives restarts (SQLite, plus the `STATE_DB` Postgres when set).

- **Watchlist** (up to 60 stocks). Alerts on them get the **Mine** filter and the gold ★, and **a MEDIUM alert on a stock you watch reaches your lock screen** (MEDIUM normally only shows in the app), marked ★ in the title. Turn that off with **Watchlist pushes**. Adding a stock also adds it to the live quotes.
- **Price alerts** (up to 40 open): above a price, below a price, or a move of ±N% on the day. Checked every 15 seconds against the freshest live price; when one hits, this phone alone gets a push (*"🔔 NVDA is above $200.00 · $200.40 (+2.10% today)"*), the app shows a toast, and tapping the push opens the stock's page. Each fires once; it then shows ✓ with the price it fired at.
- **Price alerts** also come as **trailing**: *falls N% from its high* (the high since you set it, followed on every quote).
- **Tracked calls** (up to 40 open): a paper long or short from an alert or a stock's page, at the live price when you tap. The desk shows each one's P&L live; **Levels** sets a **stop and a target** (a long's stop under the entry and target over it, a short's the other way), each pushed to this phone once when the live price reaches it (*"🎯 ACME hit your target ($110.00) · Long from $104.00, now $110.20 · +5.96%"*); **Close** books it at the live price. Closed calls build your record: how many, how many won, the average. It's how you learn which of your reads work. **The system never trades.**
- **Track record:** every alert Foretape sends is graded on its own, per stock, an hour after it, at its session's close (an after-hours alert is graded at the next session's close), at the next session's close and five sessions on (weekends and market holidays skipped; slow signals like insider buys are judged on their own timescale), in the alert's direction: + means the stock went the alert's way. The desk shows each kind's hit rate and median move (headlines by severity, radar, options, insider buys) over 30 days. Alerts without a direction count toward neither side.
- **First run:** a new phone with an empty desk opens **Make Foretape yours**: tap the stocks you follow (24 widely followed names, or type others), the words that matter to you (*tariff, FDA, buyback, guidance…*), and how much to push (*Only the big ones* = CRITICAL only on this phone, *Normal*, or *Everything, in a 15-minute digest*). **Set up from scratch** on the Desk opens it again.
- **This week** (and **your week**, pushed Fridays at 16:30 ET): calls closed this week with the record and the best one, open calls, how the alerts on your stocks did by the close, and next week: which of your stocks report and the high-impact dates.
- **Track record by first source:** which first-to-publish sources' alerts went their way (an hour later and at the close), for every source with 3+ graded headline alerts.
- **Heatmap:** your watched stocks as tiles colored by today's move, above the watchlist.
- **Saved:** ☆ Save on any alert keeps it on the Desk with its grades.
- **Snooze:** quiet a category (headlines, radar, options, insider, macro, confluence) on this phone for an hour; tap again to end it. **Skip what fades:** don't push alert kinds whose base rate says they fade (40% or less over 20+ graded); CRITICAL still comes.
- **Auto-why:** at 11:00 and 14:30 ET, a watched stock moving 4%+ with no headline behind it gets the EDGAR + news hunt (once a day per stock), so its page, the report card and the tape can say why.
- **Display (this phone):** *Cinematic* (the 3D launch scene and glass) or *Clear*: no 3D scene, no blur, solid panels, no reveal animations (legible in sunlight, light on older phones' batteries); *Larger text*; *Light* (a light theme, clear-mode based); *Compact* (more rows per screen: cards drop their secondary lines); *Vibrate on alerts* (Android: a short buzz for HIGH, CRITICAL or your stocks while the app is open); *One notification per stock* (a newer alert on a stock replaces the older one on the lock screen).
- **Keyboard and screen readers:** every tappable row is focusable and opens with Enter or Space; toasts are announced.
- **A move alert for every stock you add:** off, or ±3/5/8/10% on the day, created automatically when you watch a stock.
- **Also send to:** this phone's alerts copied to a **Discord** webhook, a **Telegram** bot chat or an **ntfy** topic: everything that reaches this phone (alerts, watchlist pushes, keyword hits, price alerts, reminders, digests), as text. The server only ever posts to those three services' own hosts; the URL or token is shown masked.
- **Notification buttons:** on Android and desktop (iOS doesn't show web-push buttons), an alert's notification has **Mute SYM 4h** next to *Open source*: it mutes the stock on that phone without opening the app.
- **Track record by time of day and "priced in":** the record also splits every graded alert by when it went out (pre-market, the open, midday, power hours, after hours) and by how far the stock had moved before it.
- **Your data:** **Backup (JSON)** downloads the whole desk; **Calls (CSV)** downloads your open and closed calls with entries, exits, P&L, levels, tags, notes and lessons.
- **Groups:** tap ⋯ on a watched stock to put it in a group (*Earnings plays*, *Semis*); the watchlist shows its groups.
- **Portfolio:** your holdings (shares and an optional cost per share): live value, today's P&L in dollars and %, unrealized P&L where you gave a cost, sector exposure as a bar, and what's moving it today. Held stocks count as yours: the Mine filter, the gold ★, and pushes even at MEDIUM.
- **Follow:** whole **sectors** and **themes** (the scorer's: AI launches, chip export controls, FDA actions, trade war, Fed policy...): their MEDIUM alerts reach this phone like a watched stock's, including theme alerts that name no stock.
- **Smart alerts** (up to 30): your own triggers on one stock or on every stock you watch (\*), pushed to this phone alone: **a headline, then an N% move within the hour** (measured from the stock's price when the headline landed), **a headline scoring N+** (N = 10 means every headline on it), **N× normal volume** for the time of day, **crosses above / loses VWAP**, **an N% pre-market gap**. Each fires once per headline, or once a day. Set them on the Desk or from any stock's page (**⚡ Smart alert**). **Preview** shows how often a rule would have fired over the last 30 days: headline rules are replayed against stored headlines; for "a headline, then a move", how many of those headlines' graded alerts moved that much within the hour; tape rules (volume, VWAP, gaps) can't be replayed (the server doesn't keep intraday prices) and say so.
- **Share your watchlist:** a code (valid 7 days, any number of uses) that adds your stocks to someone else's watchlist, grouped under the name you give it; nothing else of yours is shared.
- **Times:** ET (the market's clock) or your phone's own time zone, for the calendar, the countdown and release times.
- **What the terms mean:** every ⓘ explanation on one page.
- **Offline:** the app keeps the last tape it loaded; with no connection it opens on that, under an *Offline · showing the tape as of 9:33 PM* banner.
- **Keywords** (up to 20): your own words or phrases (*tariff*, *FDA*, *buyback*, a CEO's name), matched as whole words in every scored headline and summary. A match is pushed to this phone alone, even under the alert line (*"🔑 tariff: White House weighs new tariff on chips"*), once per story; not in your quiet hours, not when it's only about stocks you muted, and not when the story is HIGH or above (the alert itself is coming). The last hits are listed on the Desk, and tapping a keyword push searches the tape for it.
- **Journal:** a thesis or notes on any tracked call, with **#tags** (*#earnings #options #momentum*), and a lesson when you close it. The record then reads **by tag**: how many won and the average for each, so you see which kind of trade actually works for you.
- **Worth a look:** peers of your watchlist (not on it) that are doing something today: on the radar, drawing options flow or a confluence alert, or moving 3%+.
- **Board pushes:** a stock you watch that lands on the radar or the options board, with no alert of that kind on it today, reaches your phone once a day per board (*"★ ACME on the radar: +9.0% on 4× volume"*).
- **Muted:** stocks you muted, with the time left and **Unmute**.
- **Your calendar:** **Add to Calendar** subscribes Apple Calendar (webcal) to an iCalendar feed of the catalyst calendar (FOMC, data, expiries, holidays, binary events) and the next earnings date of every stock you watch, refreshed every 6 hours; **Copy the link** for Google Calendar (*Other calendars → From URL*) or Outlook. The link carries your access key, so keep it private.
- **Earnings-day reminders:** a stock you watch that reports today gets a push before it does (08:30-09:25 ET for a report before the open, 15:30-15:55 for one after the close), with what options price through the report and its average move on its last reports, when those are known.
- **The open plan:** at 09:20 ET on market days, one push with your stocks moving 1%+ pre-market, the two biggest gappers that have a reason, and today's timed high-impact events.
- **Digest mode:** *Each alert* (default), *Digest · 15 min* or *Digest · hourly*. In digest mode, alerts, watchlist pushes, keyword hits and board pushes are held and sent as one notification (*"📬 6 alerts (2 HIGH) · last 15 min"* with the top four). CRITICAL alerts, your price alerts, stops/targets, the Brief, reminders and the report card always come at once. The Desk shows how many are held.
- **Move your desk:** your desk lives with the phone (browser) it was made on. **Make a code** shows a one-time code (8 characters, no look-alike letters, valid 15 minutes); enter it under **Have a code?** on the other phone and everything (watchlist and groups, price alerts, tracked and closed calls with the journal, notes, keywords, mutes, settings) is copied there.
- **Today** (top of the Desk) and the **report card**: at 16:05 ET on market days each phone with a watchlist or tracked calls gets its day in one push: how many of its stocks rose, the best and worst, the open calls' average P&L, the price alerts and stops/targets that fired, and how Foretape's alerts on its stocks did by the close. Turn it off under *What reaches you*.
- **What reaches you:** turn categories on or off for this phone: **Headlines, Radar & ignition, Options flow, Insider buys, Macro & indexes** (an alert on SPY/QQQ/TLT-type tickers, or none), **Confluence**. **This phone's floor:** *All the server sends*, *HIGH and up*, or *CRITICAL only*, for this phone alone (your price alerts, levels, Brief and report card still come). **Quiet hours** (ET, may wrap midnight): only CRITICAL alerts reach this phone in between. These sit on top of the server-wide level and pause, which apply to every phone.

The same data is in the API: `GET /api/desk` and `POST /api/desk` (with the `X-Device` header the app sends), `GET /api/ticker/{SYM}`, `/api/search?q=`, `/api/smart`, `/api/sectors`, `/api/record?days=30`, `/api/implied/{SYM}`, `/api/peers/{SYM}`, `/api/report` (per phone), `/api/gaps?min=3`, `/api/sector?name=`, `/api/halts`, `/api/worth` (per phone), `/api/earnings/{SYM}`, `/api/calendar.ics?token=…&device=…`, `POST /api/why/{SYM}`, `/api/day`, `/api/sources`, `/api/week` (per phone), `/api/breadth`, `/api/volume`, `/api/buzz`, `/api/export` (per phone; `?format=csv` for calls), `/api/earnings-week`, `/api/inplay`, `/api/regime`, `/api/close`, `/api/similar/{alert id}`, `/api/portfolio` (per phone), `/api/tomorrow`, `/api/follow-options`, `POST /api/rule-preview` and `/api/brief` (with your stocks when the `X-Device` header is sent).

### The morning Brief

At **08:15 ET on market days**, Foretape sends one notification: *"☀️ The Brief · 3 overnight catalysts"*, with the top two headlines and anything big on the calendar today (*"Today 14:00 ET: FOMC decision"*). On a quiet night with nothing scheduled, it stays silent. Tapping it opens the Brief. Change the time with `PUSH_BRIEF_TIME`, or set it empty to turn it off.

**Lock screen:** 🔴 CRITICAL / 🟠 HIGH headline, then the tickers with ▲/▼, the source, how fast it was caught, and the strongest precedent. **Critical alerts stay on screen until you tap them.** Tapping opens that alert's analysis in the app; **Open source** jumps to the original. The app icon shows how many alerts you haven't opened, where the platform supports badges.

## The backdrop photo and fonts

- `news247/web/static/app/launch.webp` is the photo behind everything, and `depth.png` is its painted depth map (white = near). **The photo is the one you supplied. If you publish this repository, make sure you have the right to share it, or swap it for one you do.**
- To swap it: replace `launch.webp` with any portrait photo (about 1800×3200 looks best) and repaint `depth.png` at the same proportions. The shader's rocket, plume and smoke effects are placed by coordinates near the top of `scene.js` (`0.488` is the plume column; `0.43`–`0.68` is the rocket's height). A photo with different content still works: the parallax, climb into space, grain and glass need no changes.
- Fonts (Instrument Serif, Inter, JetBrains Mono, all SIL Open Font License) are bundled in `app/fonts/`, so the app makes no third-party requests and works offline once installed.

## Keeping phones subscribed on Render's free plan

Render's free plan wipes the disk on every restart. Foretape handles this in two layers:

1. **The signing key never changes.** It's derived from your `DASHBOARD_TOKEN`, so your phone's subscription stays valid across restarts and redeploys.
2. **The subscription list** lives on the server's disk. After a restart it's empty until your phone reopens Foretape, which re-registers automatically and silently. The Control tab warns you when this applies.

**For zero gaps, add a free Postgres** (for example [Neon](https://neon.tech): free, no card). In Render → news247 → Environment, set:

```
STATE_DB = postgres://user:password@host/dbname?sslmode=require
```

News247 then keeps a copy of the subscriptions there and restores them on start. It only writes when a phone subscribes or unsubscribes, so free databases that sleep when idle are fine. If you already set `WHATSAPP_STATE_DB` for the WhatsApp pairing backup, Foretape uses that one automatically.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `WEBPUSH_ENABLED` | `true` | Turn Foretape push on or off. The app itself always works. |
| `PUSH_MIN_SEVERITY` | `high` | Lowest severity sent to phones: `critical`, `high`, `medium` (more alerts). The app's Control tab can raise or lower this live. |
| `STATE_DB` | (none) | Optional Postgres URL that keeps phone subscriptions across restarts. |
| `WEBPUSH_VAPID_PRIVATE` | derived | Optional fixed signing key (base64url, 32 bytes). Only needed if you rotate `DASHBOARD_TOKEN` but want phones to stay subscribed. |
| `CONTACT_EMAIL` | (none) | Sent to push services as the operator contact; already set for SEC. |
| `PUSH_BRIEF_TIME` | `08:15` | When the morning Brief is pushed (US/Eastern, market days only). Empty turns it off. |

Quiet hours, the rate limit and the pause/level commands apply to push exactly as they do to the other channels. Several phones can subscribe; each gets every alert its own desk allows (categories, quiet hours), plus its watchlist's MEDIUM alerts and its own price alerts.

## Privacy and security

- **Encryption:** every alert is encrypted on the server for each phone (RFC 8291, "aes128gcm"). Apple and Google carry it but can't read it.
- **Signing:** each push is signed with the server's VAPID key (RFC 8292), so push services only accept pushes from your server.
- **The app shell is public; its data isn't.** `/app/` is only HTML, icons and the service worker. Alerts, settings and subscriptions all need your `DASHBOARD_TOKEN`. The token is stored on the phone so the installed app opens straight in.
- **Your desk** is stored under a random per-phone id (no name, number or email). Anyone with your access key could read a desk if they knew its id; the id is only kept on that phone and in the server's database.
- **Cleanup:** when a phone uninstalls the app or revokes permission, its push service answers *gone* and News247 deletes that subscription.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No **Turn on alerts** button on iPhone | You opened it in Safari, not from the home screen. Add it to the home screen (steps above) and open it from there. iOS must be 16.4 or newer. |
| Control tab shows **Notifications are blocked** | iPhone: Settings → Notifications → Foretape → Allow. Android/desktop: the site's settings in the browser. Then reopen the app and tap **Reconnect**. |
| Test works, but no alerts come later | Check the alert level (Control tab), quiet hours and pause state. On Render Free without `STATE_DB`, open Foretape once after a redeploy. |
| "That key didn't work" | Your `DASHBOARD_TOKEN` changed. Open `/app/?token=<new token>` once. If the phone stays subscribed to an old key, tap **Reconnect**. |
| Phones subscribed: 0 after a redeploy | Expected without `STATE_DB`: open Foretape on the phone and it re-registers. |

## How it was verified

The tests (`tests/test_webpush.py`) check:
- the encryption against the official RFC 8291 test vector, byte for byte;
- the VAPID signatures, as a push service would verify them;
- a full alert → encrypted push → decrypted on a simulated phone;
- dead-subscription cleanup and restoring subscriptions after a restart;
- the public/private split of the web routes.

An end-to-end browser run in real Chromium covered:
- tapping **Turn on alerts**;
- the engine publishing a CRITICAL alert, encrypted and signed, through a local push service;
- the service worker showing the notification, with the right title, body, "stay on screen" flag and **Open source** action.

From publish to push took about 3 ms.
