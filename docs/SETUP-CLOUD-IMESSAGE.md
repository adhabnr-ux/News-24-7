# Run News247 24/7 in the cloud and get iMessages, no Mac needed (about 15 minutes)

What you'll end up with: News247 running around the clock on a small cloud server (about $7/month), texting important market news to **your phone number as iMessages**. Your own computer can be off.

You need: a GitHub account (you have one), a credit card for the cloud host, and your iPhone.

---

## Step 1: Get a free iMessage sender (Sendblue), 5 min

1. Go to **https://sendblue.com** and sign up for the free plan. It gives you a shared iMessage number and API keys, with no card needed.
   - Prefer the terminal? `npx -y @sendblue/cli@latest setup` does the same.
2. In the Sendblue dashboard, find and copy:
   - **API Key ID**
   - **API Secret Key**
   - **your Sendblue phone number** (the "line" your messages will come from)
3. **From your iPhone, text "hi" to that Sendblue number.** On the free plan this verifies your number so Sendblue is allowed to message you. It only takes one text.

> Sendblue's free plan is meant for testing, and its pricing page is ambiguous about outbound messages. If the test message in Step 3 fails, see "If the test message fails" below.

## Step 2: Deploy to Render, 5 min

1. Click this link while signed in to GitHub:
   **https://render.com/deploy?repo=https://github.com/adhabnr-ux/News-24-7**
   (or: render.com → New → Blueprint → pick the `News-24-7` repo)
2. Render reads `render.yaml` from the repo and asks for four values. This is the only typing you do:

   | Render asks for | What to enter |
   |---|---|
   | `IMESSAGE_TO` | **your phone number**, e.g. `+15551234567` |
   | `SENDBLUE_API_KEY_ID` | from Step 1 |
   | `SENDBLUE_API_SECRET` | from Step 1 |
   | `CONTACT_EMAIL` | your e-mail (the SEC requires a contact in requests; it isn't shared anywhere else) |

3. Click **Apply** / **Deploy**. The first build takes 2–4 minutes. It runs on the **Starter** plan (~$7/month) because the free plan sleeps and would miss news.

## Step 3: Send yourself a test, 1 min

1. In Render, open the **news247** service → **Environment** and copy the value of `DASHBOARD_TOKEN` (Render generated it for you).
2. Open your service's URL with the token added, e.g.
   `https://news247-xxxx.onrender.com/setup?token=PASTE_TOKEN_HERE`
3. Check that your number is shown. You can change it here at any time. Then press **Send test message**.
4. Your iPhone should get an iMessage: "News247 test: alerts will arrive here".

Bookmark `https://news247-xxxx.onrender.com/?token=…`. That's your live dashboard (alerts, live news, market movers, source health). It works on your phone too.

That's it. From now on, important news reaches your phone within seconds of the sources publishing it.

---

## Optional: make it faster
In Render → **Environment**, add these and save. Render restarts the service automatically.

| Add | What it gives you | Cost |
|---|---|---|
| `ALPACA_ENABLED=true`, `ALPACA_KEY=…`, `ALPACA_SECRET=…` | Benzinga newsdesk headlines pushed live. Keys come from a free paper-trading account at app.alpaca.markets. | free |
| `X_STREAM_ENABLED=true`, `X_BEARER_TOKEN=…` | Posts from OpenAI, Sam Altman, Anthropic, Nvidia… texted instantly, plus squawk accounts that relay FT/WSJ/Bloomberg scoops within seconds. Token from developer.x.com (pay-per-use). | ~$10–60/month |
| `FINNHUB_TOKEN=…`, `MARKET_PROVIDER=finnhub` | Real-time stock prices for the crash detector | free |
| `PHONE_MIN_SEVERITY=critical` | Fewer texts: only the biggest events | |
| `QUIET_START=23:30`, `QUIET_END=06:30` | Overnight, only CRITICAL alerts are texted | |
| `NTFY_ENABLED=true`, `NTFY_TOPIC=some-secret-name` | A second, free push channel (install the ntfy app) as backup | free |

## If the test message fails
The Setup page shows the exact error.
- **"text your Sendblue number once…"**: you haven't texted the Sendblue number from your iPhone yet (Step 1.3).
- **"rejected the API keys"**: re-copy the key ID and secret into Render → Environment.
- **Sendblue refuses outbound messages on the free plan**: switch to **Blooio** (~$39/month, iMessage, no Mac). Sign up at blooio.com, then in Render → Environment set `BLOOIO_ENABLED=true`, `BLOOIO_API_KEY=…` and `SENDBLUE_ENABLED=false`.
- **You'd rather have plain SMS (green bubble)**: get a Textbelt key and set `TEXTBELT_ENABLED=true`, `TEXTBELT_KEY=…`. It texts the same `IMESSAGE_TO` number.

## Other hosts
- **Railway** (~$5/month): New Project → Deploy from GitHub repo → add the same variables under **Variables** → add a **Volume** mounted at `/data/data` → Settings → Networking → Generate Domain.
- **Fly.io** (~$3–4/month, needs its command-line tool): see the comments at the top of `fly.toml`.
- **Your own Mac** (free, real iMessage from your own Apple ID, but the Mac must stay on): `./deploy/install-macos.sh`.
