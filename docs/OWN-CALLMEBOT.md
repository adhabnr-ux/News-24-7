# Your own "CallMeBot", 100% free: WhatsApp alerts without Meta's business platform

**How CallMeBot works:** it runs ordinary WhatsApp accounts as **linked devices** (the same mechanism as WhatsApp Web) and messages you from them. News247 has this built in, so you don't need CallMeBot, Meta's developer platform, templates or the 24-hour rule:

```
News247 on Render Free ── linked WhatsApp device (whatsmeow engine, inside News247) ── WhatsApp ──► your phone
          │                                                                                 (normal message + sound)
          └── pairing backed up to a free Postgres (Neon), so restarts don't unlink it
```

| | Your own bot (this page) | Meta Cloud API ([FREE-SETUP.md](FREE-SETUP.md)) |
|---|---|---|
| Cost | $0 | $0 |
| Messages | full text, any time | full text only within 24 h of your last reply, otherwise a short template |
| Approvals | none | Meta app, templates, business checks (can lock: 131031) |
| Your replies (`PAUSE 2h`, `STATUS`) | always work | only when the Meta app is published |
| Needs | a **second WhatsApp number** for the bot | nothing extra |
| Risk | unofficial client: WhatsApp can restrict the **bot** account. Your own account is never involved. | none |

## The one thing it needs: a second WhatsApp number for the bot

The bot is a WhatsApp account. If it's your own account, alerts land in "Message yourself" **with no notification**. So the bot needs its own number. Free ways to get one, best first:

1. **A number you already have but don't use on WhatsApp:**
   - an old SIM that's still active;
   - a work phone;
   - a **home landline**. The **WhatsApp Business** app can verify a landline with a voice call ("Call me").
2. **Google Voice or TextNow (free).** WhatsApp rejects many of these virtual numbers, but some still go through ([reports](https://callsphere.ai/blog/google-voice-number-for-whatsapp)). Worth one try; don't request codes over and over, or WhatsApp makes you wait.
3. **A family member's unused number**, with their permission.

If none of these works, a prepaid SIM or eSIM (a few dollars) is the only remaining route, and that's no longer free.

Put the bot account in the **WhatsApp Business** app on **your own phone**. It installs next to normal WhatsApp, and your phone is then the bot's "main phone":
- WhatsApp logs out linked devices if their main phone hasn't been online for 14 days; your phone always is.
- You can see what the bot sent.

## Set it up (about 15 minutes; you already have Render running)

1. **Free Postgres for the pairing backup.**
   - Sign up at **https://neon.com** with GitHub (no card) and create a project.
   - Copy its **connection string** (`postgresql://…neon.tech/neondb?sslmode=require`).
   - It's only touched a few times an hour, well inside the free plan.
2. **Render → news247 → Environment.** Add or change these, then Save:

   | Key | Value |
   |---|---|
   | `WHATSAPP_ENABLED` | `true` |
   | `WHATSAPP_TO` | your own WhatsApp number (already set) |
   | `WHATSAPP_STATE_DB` | the Neon connection string |
   | `WHATSAPP_CLOUD_ENABLED` | `false`, or keep `true` and add `WHATSAPP_CLOUD_BACKUP=true` to use Meta only as a backup |

3. **Link the bot.**
   - Open your Setup page (`/setup?token=…`) on a computer. Under **WhatsApp sender** there's a QR code.
   - On your phone, open **WhatsApp Business** (the bot account): **Settings → Linked devices → Link a device**, and scan it.
   - Or, on the phone itself, tap **Link with phone number instead**, then type the bot's number on the Setup page and press **Get code**.
4. On your **personal** WhatsApp, save the bot as a contact ("News247") and send it "hi".
5. Press **Send test message**. A normal WhatsApp message arrives, with sound.

The Setup page shows the bot's state, *sent → delivered → read* for each alert, and **Pairing backup: saved …**. After a Render restart it says **restored after restart**, with no new scan needed.

## What News247 does to keep the bot account safe
- It messages only your number, at least 2 seconds apart, and at most 60 per hour.
- It only takes commands from your number.
- If WhatsApp ever unlinks or restricts the bot, you're told through your other channels (e.g. the Meta Cloud API as a backup, or ntfy), and a new QR code appears on the Setup page.

Like CallMeBot, this is an unofficial WhatsApp client and breaks WhatsApp's terms. Low-volume messages to one person who saved the bot and talks to it are the least likely to be flagged, but it's never risk-free. That's why the bot uses a spare number, never your own.

More detail on the linked device: [WHATSAPP.md](WHATSAPP.md).
