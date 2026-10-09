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

Foretape opens on a live sky: deep cobalt above, a gold horizon below, the way the market looks at the open. Five tabs:

| Tab | What's on it |
|---|---|
| **Tape** | The hero line, then three numbers: your **median lead over the news** (7 days), alerts in the last 24 h, and sources live. Then the **next big catalyst** on the calendar. Below that, **Top of the tape** (the strongest alert of the last 6 h, in large type) and every other alert. Tap any alert for its full dossier. |
| **Brief** | The morning meeting: catalysts **since the last close**, the themes in focus, SPY/QQQ/IWM/DIA and the biggest movers, the week's calendar, and **your edge** (stories you had before the mainstream, median and biggest head start). |
| **Calendar** | Scheduled catalysts for the next 60 days, with time (ET) and impact: FOMC decisions, jobs reports, elections, option expirations and quad witching, market holidays and early closes, plus one-offs like the end of China's rare-earth suspension. Edit `news247/data/calendar.yaml` to add your own (earnings dates, investor days). |
| **Watch** | The board: every quoted symbol sorted by today's move, with the 5-minute change. Tickers from fresh alerts are added automatically. |
| **Desk** | Push status, **Send a test alert**, **Reconnect**, alert level (**Critical only / Normal / More**), **Pause 30m / 2h / until resumed**, **Resume**, the Brief time, and engine health. |

### Every alert comes with an analysis

| Part | What it tells you |
|---|---|
| **The play** | **▲ Bullish / ▼ Bearish / ◆ Two-way**, a 5-bar **conviction** meter (Low → Maximum, from the score), the **direct** tickers and the **read-through** names (suppliers, competitors, the sector basket). When the headline itself doesn't say which way it cuts (a Fed hike, a tariff post, a Polymarket jump), the direction comes from how the most similar past events traded, and the app says so. |
| **Precedents** | The most similar market-moving events on record (150+ since 2016, including every Jun–Oct 2026 mover we traced), matched on the same themes, entities and tickers the scorer found. Each shows what the stocks did then. Example: an FHFA post about VantageScore → *"Last time (Sep 28, 2026): FICO −25 to −27%"*. The top precedent is also on the lock-screen notification. |
| **The tape since** | Each direct ticker's move since the alert ("FICO −9.76% since alert"), live from the price feed. |
| **The edge** | How many seconds after publication Foretape caught it, where it was first seen, and when the mainstream caught up: *"Beat CNBC by 6m 52s"*. It's measured live: when CNBC, Yahoo Finance, Google News, MarketWatch, WSJ, Bloomberg, NYT, Fortune or Axios later carries the same story, the gap is recorded. |
| **Who carried it** | Every source that carried the story, first one first, with the gaps. |
| **Why Foretape flagged it** | The full score breakdown: every keyword, theme, authority and source signal that counted. |

### The morning Brief

At **08:15 ET on market days**, Foretape sends one notification: *"☀️ The Brief · 3 overnight catalysts"*, with the top two headlines and anything big on the calendar today (*"Today 14:00 ET: FOMC decision"*). On a quiet night with nothing scheduled, it stays silent. Tapping it opens the Brief. Change the time with `PUSH_BRIEF_TIME`, or set it empty to turn it off.

**Lock screen:** 🔴 CRITICAL / 🟠 HIGH headline, then the tickers with ▲/▼, the source, how fast it was caught, and the strongest precedent. **Critical alerts stay on screen until you tap them.** Tapping opens that alert's analysis in the app; **Open source** jumps to the original. The app icon shows how many alerts you haven't opened, where the platform supports badges.

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
