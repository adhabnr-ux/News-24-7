# Research: 24/7 hosting and iMessage without a Mac (2026)

Compiled 2026-10-08 via web search; vendor docs could not be fetched directly. Items marked [unverified] conflict between sources.

## Hosting
| Provider | ~Cost/month | Sleeps? | Persistent disk | Ease | Notes |
|---|---|---|---|---|---|
| **Render** Starter + disk | $7 + $0.25/GB | No (paid) | Yes | **Easy**: Blueprint `render.yaml`, which prompts for secrets (`sync: false`) | Free web services sleep after 15 min, so don't use them |
| **Railway** Hobby | $5 (usage included) | No | Volumes | Easy: auto-detects `Dockerfile` | `railway.json` config-as-code is deprecated (stops 2026-12-01), so we ship none |
| **Fly.io** shared-cpu-1x 512MB | ~$3.2 + volume | No (`auto_stop_machines="off"`) | Volumes | Medium (CLI) | No free allowance for new orgs |
| Google Cloud e2-micro | $0 | No | 30 GB | Hard (VM) | Always Free in us-west1/central1/east1 |
| Oracle Always Free A1 | $0 | **Reclaimed when idle** | Yes | Hard | A1 quota halved in 2026; a low-CPU monitor risks reclamation |
| DigitalOcean / Vultr / Hetzner | $2.50–6 | No | Yes | Hard (VM) | |
| Koyeb free / Render free / Railway free | $0 | **Yes** | No/limited | | Not suitable for 24/7 |

Sources: [Render free](https://render.com/docs/free), [Render YAML spec](https://render.com/docs/yaml-spec), [Railway pricing](https://docs.railway.com/pricing/plans), [Railway config-as-code](https://docs.railway.com/config-as-code), [Fly pricing](https://fly.io/docs/about/pricing/), [Oracle free tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/resourceref.htm), [GCP free](https://cloud.google.com/free/docs/compute-getting-started).

## iMessage without a Mac
| Service | Price | Notes |
|---|---|---|
| **Sendblue** | Free sandbox: shared number, up to 10 verified contacts; paid "AI Agent" plan ~$100/line/mo | The recipient must text the Sendblue number once to become verified. **The pricing page lists "no outbound messaging" for free while the quickstart documents sends to verified contacts**; this conflicts [unverified], so test it. API: `POST /api/send-message` with headers `sb-api-key-id` / `sb-api-secret-key` and body `{number, content, from_number}`. Docs show both `api.sendblue.co` and `api.sendblue.com`; News247 tries both and looks up `from_number` via `/api/lines` ([docs](https://docs.sendblue.com/getting-started/sending-messages)) |
| **Blooio** | ~$39/mo shared, unlimited | `POST https://backend.blooio.com/v2/api/chats/{E.164}/messages`, Bearer key, `{text}` ([docs](https://docs.blooio.com/guides/imessage-rest-api)) |
| LoopMessage, Linq, Claw Messenger | $5 to $250+/mo | Unclear docs or sales-only [unverified] |
| BlueBubbles | Free | Needs a Mac at home as a relay: `POST /api/v1/message/text?password=…` with `chatGuid "iMessage;-;+1…"`, or `/api/v1/chat/new` |

## Without iMessage
- **Plain SMS:**
  - **Textbelt** costs ~$0.06/text with no 10DLC paperwork, but URLs need whitelisting.
  - **Twilio** sole-proprietor 10DLC takes ~$20 in fees plus $2/mo, with 10–15 days of campaign review.
- **Carrier email-to-SMS gateways are dead:** AT&T shut down June 2025, T-Mobile stopped working, and Verizon sunsets Mar 2027.
- **E-mail to an Apple ID does not appear in Messages.**
- **Push apps** (ntfy, free; Pushover, $5 one-time) are the most reliable fallback.

## Native macOS (if a Mac can stay on)
- Use AppleScript `participant <handle> of (1st account whose service type = iMessage)` on macOS 11+, including macOS 26 Tahoe.
- Fall back to `buddy … of service` on older macOS, then to `chat id "iMessage;-;…"`.
- **Messages to your own number may not notify**, so sign the Mac into a separate "bot" Apple ID.
- Run as a LaunchAgent, not a daemon, and approve Automation once.

Sources: [BlueBubbles source](https://github.com/BlueBubblesApp/bluebubbles-server/blob/master/packages/server/src/server/api/apple/scripts.ts), [scriptingosx](https://scriptingosx.com/2020/09/avoiding-applescript-security-and-privacy-requests/).
