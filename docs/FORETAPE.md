# Foretape: News247 on your home screen, with instant push alerts

**Foretape** ("before the tape moves") is News247's own phone app. It's a website you add to your home screen, so it looks and behaves like an app:

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

| Where | What |
|---|---|
| **Lock screen** | 🔴 CRITICAL / 🟠 HIGH headline, then the tickers with ▲/▼, the source, how many seconds after publication it was caught, and why it matters. **Critical alerts stay on screen until you tap them.** Tap to open that alert in the app; **Open source** jumps to the original. |
| **Alerts tab** | Every alert, newest first: severity, source, tickers, "⚡ caught 3s after it was posted", and a link to the source. The ticker tape at the top scrolls the latest headlines. |
| **Wire tab** | The latest 60 stories News247 scored as at least MEDIUM, including ones below the alert line. Useful to see what it's watching. |
| **Control tab** | **Send a test alert**, **Reconnect** (re-registers this phone), alert level (**Critical only / Normal / More**), **Pause 30m / 2h / until resumed**, **Resume**, plus engine health: healthy sources, uptime, alerts in 24 h, and phones subscribed. |

The app icon shows a badge with the number of alerts you haven't opened yet, where the platform supports it.

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
