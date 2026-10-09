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
| **Tape** | The hero line, then three numbers: your **median lead over the news** (7 days), alerts in the last 24 h, and sources live. Then the **next big catalyst** on the calendar. Below that, **Top of the tape** (the strongest alert of the last 6 h, in large type) and every other alert. Tap any alert for its full dossier. The **All / Small caps** switch above the tape filters to small- and mid-cap catalysts and radar hits (remembered on the phone). |
| **Brief** | The morning meeting: catalysts **since the last close**, the themes in focus, SPY/QQQ/IWM/DIA and the biggest movers, the week's calendar, and **your edge** (stories you had before the mainstream, median and biggest head start). |
| **Calendar** | Scheduled catalysts for the next 60 days, with time (ET) and impact: FOMC decisions, jobs reports, elections, option expirations and quad witching, market holidays and early closes, plus one-offs like the end of China's rare-earth suspension. Edit `news247/data/calendar.yaml` to add your own (earnings dates, investor days). |
| **Watch** | The **small-cap radar** first: small caps breaking out right now on real volume (±20% on the day or a fast jump, $2M+ traded), with market cap, dollars traded, relative volume, pre-market/after-hours tag and the headline behind the move, or "No headline yet". Then the board: every quoted symbol sorted by today's move, with the 5-minute change. Tickers from fresh alerts are added automatically. |
| **Desk** | Push status, **Send a test alert**, **Reconnect**, motion on/off, alert level (**Critical only / Normal / More**), **Pause 30m / 2h / until resumed**, **Resume**, the Brief time, and engine health. |

### Every alert comes with an analysis

| Part | What it tells you |
|---|---|
| **The little thing** | For a small or mid cap: the company, its market cap and size band, the catalyst, the typical move **for a company that size** (*"Contract from U.S. Army worth 64% of market cap · +36–96% typical"*) and the deal size as a share of the company. On cards it is the gold **MICRO CAP / SMALL CAP / MID CAP** pill; radar alerts carry **◉ Radar** with relative volume. See [SMALLCAPS.md](SMALLCAPS.md). |
| **The play** | **▲ Bullish / ▼ Bearish / ◆ Two-way**, a 5-bar **conviction** meter (Low → Maximum, from the score), the **direct** tickers and the **read-through** names (suppliers, competitors, the sector basket). When the headline itself doesn't say which way it cuts (a Fed hike, a tariff post, a Polymarket jump), the direction comes from how the most similar past events traded, and the app says so. |
| **Precedents** | The most similar market-moving events on record (150+ since 2016, including every Jun–Oct 2026 mover we traced), matched on the same themes, entities and tickers the scorer found. Each shows what the stocks did then. Example: an FHFA post about VantageScore → *"Last time (Sep 28, 2026): FICO −25 to −27%"*. The top precedent is also on the lock-screen notification. |
| **The tape since** | Each direct ticker's move since the alert ("FICO −9.76% since alert"), live from the price feed. |
| **The edge** | How many seconds after publication Foretape caught it, where it was first seen, and when the mainstream caught up: *"Beat CNBC by 6m 52s"*. It's measured live: when CNBC, Yahoo Finance, Google News, MarketWatch, WSJ, Bloomberg, NYT, Fortune or Axios later carries the same story, the gap is recorded. |
| **Who carried it** | Every source that carried the story, first one first, with the gaps. |
| **Why Foretape flagged it** | The full score breakdown: every keyword, theme, authority and source signal that counted. |

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

Quiet hours, the rate limit and the pause/level commands apply to push exactly as they do to the other channels. Several phones can subscribe; each gets every alert.

## Privacy and security

- **Encryption:** every alert is encrypted on the server for each phone (RFC 8291, "aes128gcm"). Apple and Google carry it but can't read it.
- **Signing:** each push is signed with the server's VAPID key (RFC 8292), so push services only accept pushes from your server.
- **The app shell is public; its data isn't.** `/app/` is only HTML, icons and the service worker. Alerts, settings and subscriptions all need your `DASHBOARD_TOKEN`. The token is stored on the phone so the installed app opens straight in.
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
