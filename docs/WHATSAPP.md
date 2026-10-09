# WhatsApp alerts from your own server (self-hosted, 24/7)

> Want it **100% free**? Use Meta's free test number with the Cloud API instead: [FREE-SETUP.md](FREE-SETUP.md). This page covers the linked-device option, which needs a persistent disk (a paid host) and ideally a second number.

News247 can send alerts to your WhatsApp **from the server it already runs on**. There's no third-party messaging service, no Mac, and no computer of yours that has to stay on.

```
 News sources ──► News247 (cloud, 24/7) ── linked WhatsApp device inside the same process ──► WhatsApp servers ──► your phone
                                             (like WhatsApp Web, but headless)                                        (normal notification)
```

## How it works

WhatsApp lets every account link up to four extra devices (WhatsApp Web, Desktop…). News247 becomes one of them:

- **Pairing:** you pair it **once** by scanning a QR code or typing an 8-character code.
- **Connection:** it keeps its own end-to-end-encrypted connection to WhatsApp, sends alerts from that account, and receives your replies.
- **Engine:** [whatsmeow](https://github.com/tulir/whatsmeow), the Go library behind mautrix-whatsapp, used through its Python binding [neonize](https://pypi.org/project/neonize/). It runs inside the News247 process and needs ~50–100 MB of RAM. The Docker image includes it.
- **Pairing storage:** the pairing is kept on the server's persistent disk (`<data_dir>/whatsapp/session.db`), so restarts and redeploys don't unlink it.

What News247 adds on top:

| | |
|---|---|
| **Supervisor** | Reconnects by itself after network blips. If the engine crashes, it's restarted with backoff. If the phone unlinks it, a fresh QR code appears on the Setup page and you're alerted through your other channels (e.g. ntfy). |
| **Receipts** | Every alert shows *sent → delivered → read* on the Setup page, plus the median time until it reached your phone. |
| **Account protection** | Messages go out one at a time, at least 2 s apart, and at most 60 per hour. They only go to your own number. Only your number may send commands. |
| **Text commands** | Reply `PAUSE 2h`, `STOP`, `RESUME`, `CRITICAL`, `MORE`, `NORMAL`, `STATUS`, `HELP`. |
| **Backup** | Pair it with a backup channel (e.g. ntfy, `NTFY_BACKUP=true`). If WhatsApp can't send (not linked, reconnecting, restricted), the alert goes there immediately. |

## Two accounts: who sends, who receives

A message your own account sends to your own number lands in **"Message yourself"**, **without a notification**. So the sender should be a **second WhatsApp account**:

| Sender option | Cost | Notes |
|---|---|---|
| **WhatsApp Business app on your own iPhone/Android, with a second number** | a prepaid SIM or eSIM (~$3–10/month), or a landline (WhatsApp Business can verify by voice call) | **Recommended.** WhatsApp and WhatsApp Business run side by side on one phone. Your phone is the sender's "primary device" and is always online, so the link never expires. |
| A spare phone with its own number | the SIM | It must come online at least every 14 days: WhatsApp logs out linked devices when the primary phone has been inactive that long. |
| Your own account | free | Works, but the alerts arrive silently in "Message yourself". The Setup page warns you about this. |

Then, on **your** phone, save the sender's number as a contact (e.g. "News247") and send it a message once. Two-way, saved contacts look like a normal conversation to WhatsApp, which helps keep the sender account in good standing.

## Honest risk note

Linking an unofficial client like whatsmeow is **against WhatsApp's terms of service**, and WhatsApp can restrict accounts it thinks are automated. Restrictions usually start as a temporary ban. Reports suggest bots that only message someone who knows them, at low volume, are rarely affected. Bulk or unsolicited messaging is what gets accounts banned. News247 sends only to you, spaced out and capped. Still:

- **Don't use your main number as the sender.** Use the second number above, so a restriction never touches your personal WhatsApp.
- If WhatsApp restricts the sender, News247 shows it on the Setup page and alerts you through your backup channel.
- The zero-risk alternative is Meta's **official Cloud API** (below). It has its own rules.

## Set it up

1. Deploy (see [SETUP-CLOUD.md](SETUP-CLOUD.md)) with `WHATSAPP_ENABLED=true` and `WHATSAPP_TO=<your number>`. The Render blueprint does both.
2. Open `https://<your-server>/setup?token=<DASHBOARD_TOKEN>`. Under **WhatsApp sender** you'll see a QR code.
3. On the phone with the **sender** account: WhatsApp (Business) → **Settings → Linked devices → Link a device**.
   - Scan the QR, **or**
   - if the Setup page is open on that same phone, tap **Link with phone number instead**, type the sender's number on the Setup page, press **Get code**, and enter the 8-character code in WhatsApp.
4. The Setup page switches to **Connected as +1…**. Press **Send test message**.

Local or other hosts: `pip install 'news247[whatsapp]'`. On Debian/Ubuntu also install `libmagic1`; on macOS run `brew install libmagic`. Then `WHATSAPP_ENABLED=true WHATSAPP_TO=+1… news247 run` and open http://localhost:8247/setup.

### Settings (`notify.whatsapp`)

| Option | Default | |
|---|---|---|
| `enabled` | `${WHATSAPP_ENABLED:-false}` | |
| `to` | `${WHATSAPP_TO}` | Your number(s). The Setup page's number field overrides it. |
| `min_interval_s` | `2` | Spacing between messages |
| `max_per_hour` | `60` | Hard cap; command replies don't count |
| `session_dir` | `<data_dir>/whatsapp` | Must be on persistent storage |
| `min_severity` | `${PHONE_MIN_SEVERITY:-high}` | |
| `backup` | `${WHATSAPP_BACKUP:-false}` | Make WhatsApp the backup for another channel instead |

## The official alternative: WhatsApp Cloud API (`whatsapp_cloud`)

Meta hosts the sender, and there's no ban risk. The trade-offs:

- Meta lets a business message you freely only within **24 hours of your last message** to it. Outside that window, only **pre-approved templates** can be sent.
- New production numbers start with a daily limit.

Setup:
1. Go to developers.facebook.com → **Create app** (type *Business*) → add **WhatsApp**.
2. **API Setup** gives you a free **test number** and its **Phone number ID**. Add your own number as a test recipient (up to 5).
3. Create a permanent token: Business Settings → **System users** → add one → generate a token with `whatsapp_business_messaging`. The token shown on the API Setup page expires after 24 h.
4. *(For alerts outside the 24-hour window)* WhatsApp Manager → **Message templates** → create a *Utility* template, e.g. `news247_alert`, body `News247: {{1}}`. Once it's approved:
   - set `WHATSAPP_CLOUD_TEMPLATE=news247_alert`;
   - News247 sends free-form text when it can, and falls back to the template (alert squeezed onto one line) when Meta answers with error 131047.
5. Set `WHATSAPP_CLOUD_ENABLED=true`, `WHATSAPP_CLOUD_TOKEN=…`, `WHATSAPP_CLOUD_PHONE_ID=…`, `WHATSAPP_TO=+1…`.

Tip: send the business number any message each morning to open a free 24-hour window.

Using both together works well: `whatsapp` as the main channel and `whatsapp_cloud` with `WHATSAPP_CLOUD_BACKUP=true`. If the linked device is ever restricted or unlinked, alerts keep arriving through Meta's API.

## Troubleshooting

| Setup page shows | Fix |
|---|---|
| *Engine not installed* | The Docker image includes it. Elsewhere: `pip install 'news247[whatsapp]'` and install libmagic (see above). |
| *Waiting to be linked* (no QR) | The server can't reach WhatsApp yet. Wait a few seconds; the error line says why. |
| *Unlinked: link it again* | The phone removed the device, or the sender's phone was offline for 14+ days. Pair again. |
| *Restricted by WhatsApp* | A temporary ban on the sender account. Alerts go to your backup channel in the meantime. Consider the Cloud API. |
| Messages arrive without a sound | The sender is your own account ("Message yourself"). Link a second number. |
| `hourly cap of 60 … reached` | Raise `max_per_hour`, or send fewer alerts (`PHONE_MIN_SEVERITY=critical`). |

Code: [`news247/whatsapp/`](../news247/whatsapp/). Tests: [`tests/test_whatsapp.py`](../tests/test_whatsapp.py). They cover pairing, spacing and caps, receipts, commands, logout and re-pairing, crash recovery, reconnect waits, the web endpoints, the Cloud API's template fallback, and booting the real engine when it's installed.
