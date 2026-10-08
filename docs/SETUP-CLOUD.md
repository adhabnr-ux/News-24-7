# Run News247 24/7 in the cloud with alerts on WhatsApp (about 15 minutes)

**What you end up with:**
- News247 runs around the clock on a small cloud server (about $7/month), so your own computer can be off.
- That **same server** sends important market news to **your WhatsApp**, as a linked WhatsApp device it runs itself. There's no third-party messaging service. How it works: [WHATSAPP.md](WHATSAPP.md).

**You need:** a GitHub account, a credit card for the cloud host, and WhatsApp. **Best:** a second number for the sending account, e.g. the WhatsApp Business app on your phone with a cheap second SIM/eSIM, so alerts arrive with a notification. See [why](WHATSAPP.md#two-accounts-who-sends-who-receives).

## Step 1: Deploy to Render (5 min)

1. Click this link while signed in to GitHub:
   **https://render.com/deploy?repo=https://github.com/adhabnr-ux/News-24-7**
   (or render.com → New → Blueprint → pick the `News-24-7` repo)
2. Render reads `render.yaml` and asks for two values:

   | Render asks for | What to enter |
   |---|---|
   | `WHATSAPP_TO` | **your** WhatsApp number (where alerts go), e.g. `+16235551234` |
   | `CONTACT_EMAIL` | your e-mail. The SEC requires a contact in requests; it isn't shared anywhere else. |

3. Click **Apply** / **Deploy**. The first build takes 3–5 minutes. It runs on the **Starter** plan (~$7/month), because the free plan sleeps and would miss news. The included disk keeps the WhatsApp pairing across restarts.

## Step 2: Link the sending WhatsApp (3 min)

1. In Render, open the **news247** service → **Environment**, and copy the value of `DASHBOARD_TOKEN`.
2. Open `https://news247-xxxx.onrender.com/setup?token=PASTE_TOKEN_HERE`, preferably on a computer.
3. Under **WhatsApp sender** there's a QR code. On the phone with the **sending** account (WhatsApp Business with your second number, ideally): **Settings → Linked devices → Link a device**, and scan it.
   - If the page is open on that same phone: tap **Link with phone number instead**, type the sender's number on the page, press **Get code**, and enter the code in WhatsApp.
4. It switches to **Connected as +1…**.
5. On your **personal** WhatsApp, save the sender's number as a contact (e.g. "News247") and send it "hi".

## Step 3: Test it (1 min)

Press **Send test message**. Your WhatsApp gets **"🔴 News247 test: alerts will arrive here"**. The Setup page then shows it as *delivered*, and *read* once you open it.

Bookmark `https://news247-xxxx.onrender.com/?token=…`. It's your live dashboard, and it works on your phone too.

## Control it from WhatsApp

Reply to any alert:

| Reply | Effect |
|---|---|
| `PAUSE 2h` | Pause alerts for a set time (`30m`, `1d`…) |
| `STOP` | Pause until `RESUME` |
| `RESUME` | Alerts back on |
| `CRITICAL` | Only the biggest events |
| `MORE` | Medium alerts too |
| `NORMAL` | Back to the default |
| `STATUS` | Is everything running? |

## Recommended: a free backup (2 min)

If WhatsApp can't send (relinking, a restriction), alerts should still reach you:
1. Install the **ntfy** app and subscribe to a hard-to-guess topic, e.g. `news247-k7f3q9x2`.
2. In Render → **Environment**, add `NTFY_ENABLED=true`, `NTFY_BACKUP=true`, `NTFY_TOPIC=news247-k7f3q9x2`.

The backup only fires when WhatsApp failed. You're also told there if WhatsApp gets unlinked.

## Optional: make it faster

| Add (Render → Environment) | What it gives you | Cost |
|---|---|---|
| `ALPACA_ENABLED=true`, `ALPACA_KEY=…`, `ALPACA_SECRET=…` | Benzinga newsdesk headlines pushed live (free paper-trading keys from app.alpaca.markets) | free |
| `X_STREAM_ENABLED=true`, `X_BEARER_TOKEN=…` | Posts from OpenAI, Sam Altman, Anthropic, Nvidia… sent instantly, plus squawk accounts that relay FT/WSJ/Bloomberg scoops within seconds | ~$10–60/month |
| `FINNHUB_TOKEN=…`, `MARKET_PROVIDER=finnhub` | Real-time prices for the crash detector | free |
| `PHONE_MIN_SEVERITY=critical` | Fewer messages: only the biggest events | |
| `QUIET_START=23:30`, `QUIET_END=06:30` | Overnight, only CRITICAL alerts | |

## Prefer zero ban risk?
Use Meta's official WhatsApp Cloud API instead of, or as a backup to, the linked device: [WHATSAPP.md → official alternative](WHATSAPP.md#the-official-alternative-whatsapp-cloud-api-whatsapp_cloud).

## iMessage too?
Any Mac signed in to Messages can be a sender as well: [SETUP-CLOUD-IMESSAGE.md](SETUP-CLOUD-IMESSAGE.md).

## Other hosts
- **Railway** (~$5/month): New Project → Deploy from GitHub repo → add `WHATSAPP_ENABLED=true`, `WHATSAPP_TO`, `CONTACT_EMAIL`, `DASHBOARD_TOKEN` under **Variables** → add a **Volume** at `/data/data` (keeps the pairing) → Settings → Networking → Generate Domain.
- **Fly.io** (~$3–4/month): see the comments at the top of `fly.toml`.
