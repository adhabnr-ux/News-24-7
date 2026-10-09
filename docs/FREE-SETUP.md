# 100% free: News247 running 24/7, alerts on your WhatsApp (about 30 minutes, once)

**What you end up with:**
- News247 runs around the clock on **Render's Free plan**. It pings itself every 10 minutes so Render never puts it to sleep.
- Alerts arrive on **your WhatsApp** from **Meta's free WhatsApp test number**, through the official Cloud API: no SIM to buy, no card, and no ban risk.
- **Total cost: $0.** No credit card is needed for either service.

You need: a GitHub account (you have one), a Facebook account (for Meta's developer site), and WhatsApp on your phone.

## How the free WhatsApp number behaves

Meta's rule: a business can send you **normal messages only within 24 hours of your last message to it**. News247 handles that for you:

| Situation | What you get |
|---|---|
| You've messaged the News247 number in the last 24 h | The full alert: bold headline, tickers, why it matters, link |
| You haven't | A short alert (headline + tickers on one line) with a **Show details** button. Tapping it reopens the 24 h window and you get every alert you missed, in full. |
| The very first hour, while Meta reviews News247's template | A "Hello World" ping from Meta's built-in template. Reply anything and the waiting alerts arrive in full. |

Tip: replying to anything (`STATUS`, a 👍) reopens the window for another 24 h.

---

## Part 1: Meta (about 20 minutes)

1. **Become a Meta developer:** go to **https://developers.facebook.com**, log in with Facebook, click **Get started**, and confirm your e-mail/phone.
2. **Create the app:** **My Apps → Create App**. Pick the use case **"Connect with customers through WhatsApp"** (or *Other → Business*), name it **News247**, and create it. If asked for a Business portfolio, let it create one called "News247".
3. **WhatsApp → API Setup** (left menu). Meta has already given you a **test number** (the "From" field). Copy two numbers from this page:
   - **Phone number ID** → this becomes `WHATSAPP_CLOUD_PHONE_ID`
   - **WhatsApp Business Account ID** → this becomes `WHATSAPP_CLOUD_WABA_ID`
4. **Allow your phone to receive messages:**
   - In the **To** field choose **Manage phone number list → Add phone number**, and enter **your WhatsApp number**.
   - WhatsApp sends you a code; type it in.
   - Click **Send message**. A "Hello World" from Meta arrives on your WhatsApp.
   - **Reply "hi" to it.** Save the sender as a contact named "News247".
5. **Make a token that never expires.** The one shown on the API Setup page dies after 24 h.
   - Go to **https://business.facebook.com → Settings → Users → System users → Add**, name it "news247", role **Admin**.
   - **Assign assets:** the **News247 app** (full control) and your **WhatsApp account** (full control).
   - **Generate new token:** pick the News247 app, set expiration to **Never**, and tick **whatsapp_business_messaging** and **whatsapp_business_management**.
   - Copy it. This becomes `WHATSAPP_CLOUD_TOKEN`.
6. **App secret:** back in the app, open **App settings → Basic → App secret → Show** and copy it. This becomes `WHATSAPP_CLOUD_APP_SECRET`.

## Part 2: Render (about 5 minutes)

1. Open **https://render.com/deploy?repo=https://github.com/adhabnr-ux/News-24-7** and sign up or log in with GitHub. The Free plan needs no card.
2. Render asks for these values:

   | Field | Value |
   |---|---|
   | `WHATSAPP_TO` | your WhatsApp number with country code, e.g. `+16235551234` |
   | `CONTACT_EMAIL` | your e-mail. The SEC requires a contact; it isn't shared anywhere else. |
   | `WHATSAPP_CLOUD_TOKEN` | the token from step 5 |
   | `WHATSAPP_CLOUD_PHONE_ID` | the Phone number ID from step 3 |
   | `WHATSAPP_CLOUD_WABA_ID` | the WhatsApp Business Account ID from step 3 |
   | `WHATSAPP_CLOUD_APP_SECRET` | the app secret from step 6 |

3. Click **Apply**. The first build takes about 5 minutes.
4. When it's live, open **news247 → Environment** in Render and copy `DASHBOARD_TOKEN`. Open `https://<your-app>.onrender.com/setup?token=<DASHBOARD_TOKEN>` and bookmark it.

On first start News247 checks your number and **creates its alert template** (`news247_alert`) for Meta to review. The Setup page shows the review status; utility templates are usually approved within minutes.

## Part 3: Connect your replies (about 3 minutes)

This lets News247 see your replies, so "Show details" and commands like `PAUSE 2h` work, and it shows delivered/read ticks.

1. The Setup page's WhatsApp section shows a **Callback URL** and a **Verify token**.
2. In the Meta app: **WhatsApp → Configuration → Webhook → Edit**. Paste both and click **Verify and save**.
3. Under **Webhook fields**, click **Manage** and **subscribe to `messages`**.
4. On the Setup page, press **Send test message**. You should get "🔴 News247 test: alerts will arrive here". The page then shows *delivered*, then *read*.

That's it. It runs 24/7 at no cost.

## Control it from WhatsApp

| Reply | Effect |
|---|---|
| `PAUSE 2h` / `STOP` / `RESUME` | Pause for a while, pause until resumed, or turn alerts back on |
| `CRITICAL` / `MORE` / `NORMAL` | Only the biggest events, medium ones too, or the default |
| `STATUS` | Is everything running? |
| `DETAILS` | Full text of alerts you only saw as short versions |

## Optional

- **Belt and braces for "never sleeps":** a free monitor at uptimerobot.com pointed at `https://<your-app>.onrender.com/health` every 5 minutes. It also e-mails you if News247 is ever down.
- **Faster news (free):** `ALPACA_ENABLED=true` + free keys from app.alpaca.markets (Benzinga headlines pushed live). Add them under Render → Environment.
- **Free push backup:** install the ntfy app and set `NTFY_ENABLED=true`, `NTFY_BACKUP=true`, `NTFY_TOPIC=<secret topic>`.

## Limits worth knowing
- **Render Free:**
  - 512 MB RAM and a small CPU share; plenty for News247's alerting.
  - **750 free hours a month** covers one service running all month (744 h max). Don't run a second free service in the same Render workspace.
  - The disk is temporary: the alert history on the dashboard resets when Render restarts or redeploys. Alerts themselves are unaffected.
- **Meta test number:**
  - Up to 5 recipient numbers, no payment method needed.
  - Meta's pricing for messages can change ([Meta](https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started)). The test number doesn't require a payment method, so you can't be charged.
- **Template wording:** the alert template reads "📈 News247 alert: <headline · tickers> — tap below for the full details." Meta doesn't allow a template to start or end with the variable part, so the fixed words stay.

## If Meta says "Business Account locked" (131031)

On brand-new setups Meta's automated checks often lock the WhatsApp Business Account before a single real message has gone out. The usual triggers are a business profile without a working website or legal name, and an unpublished app. Meta's free test numbers are also known to get stuck in this state. News247 gives you everything Meta asks for. Your server serves these public pages (replace the address with yours):

| Page | Address |
|---|---|
| Website | `https://<your-app>.onrender.com/about` |
| Privacy policy (with data-deletion instructions) | `https://<your-app>.onrender.com/privacy` |
| Terms of service | `https://<your-app>.onrender.com/terms` |

1. **See Meta's exact reason.** The Setup page's WhatsApp section shows **Meta health check: blocked**, plus Meta's own error description and suggested fix for each part (app, business, WhatsApp account, number).
2. **Complete Business info.** Go to business.facebook.com → **Settings → Business info → Edit**.
   - **Legal business name:** your full name. A personal business portfolio is fine.
   - Add your address and phone number.
   - **Website:** the `/about` address above.
3. **Publish the app.** On developers.facebook.com, open the News247 app.
   - Under **App settings → Basic**, set:
     - **Privacy policy URL:** `/privacy`
     - **Terms of service URL:** `/terms`
     - **User data deletion → Data deletion instructions URL:** `/privacy#deletion`
     - **Category:** any; Business and pages works.
     - **App icon:** any square image.
   - **Save**, then switch **App mode** to **Live** (or click **Publish**).
   - Publishing your own app for your own WhatsApp account doesn't normally need business verification. If the dashboard insists, it says exactly what's missing.
   - Published apps also receive your real replies through the webhook. Unpublished apps only get Meta's test events.
4. **Ask for a review.** Go to business.facebook.com → **Account Quality** (or WhatsApp Manager → Overview). If your WhatsApp account shows **Request review**, use it and say it's a personal alert tool that only messages its owner.
5. Wait for the Setup page's health check to turn **available**. It re-checks every 2 minutes while there's a problem. Then press **Send test message**.
6. **Still locked after a day or two?** Meta's test numbers sometimes stay stuck. Your business info is complete now, so create a **new app** with the WhatsApp use case inside the same business portfolio; it gets a fresh test number. Then:
   - add your number as a recipient again;
   - give the system user access to the new app and WhatsApp account, and generate a new token;
   - in Render, update `WHATSAPP_CLOUD_TOKEN`, `WHATSAPP_CLOUD_PHONE_ID`, `WHATSAPP_CLOUD_WABA_ID` and `WHATSAPP_CLOUD_APP_SECRET`;
   - set the webhook up in the new app.

## Troubleshooting

| Setup page shows | Fix |
|---|---|
| `error 190 … access token` | The token expired or was mistyped. Make the System User token again (expiration **Never**) and update `WHATSAPP_CLOUD_TOKEN`. |
| `error 131030 … not in allowed list` | Add your number under API Setup → **To → Manage phone number list**. |
| Template `rejected` | Delete it in WhatsApp Manager → Message templates. News247 recreates it on the next restart, or set `WHATSAPP_CLOUD_TEMPLATE` to a new name. |
| Webhook "nothing received yet" | Check the Callback URL and Verify token in Meta → WhatsApp → Configuration, and that **messages** is subscribed. |
| Alerts arrive as "Hello World" | The template is still in review. Reply anything to get the alerts in full. |
| `error 131031 … Business Account locked` | See [If Meta says "Business Account locked"](#if-meta-says-business-account-locked-131031) above. |
| Meta test webhook works but your real replies don't arrive | The app is unpublished: publish it (step 3 of the section above). |
