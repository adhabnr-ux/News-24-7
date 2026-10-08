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

## Building our own sender (researched 2026-10-08)
- **No official route.**
  - Apple publishes no iMessage-sending API.
  - Apple Messages for Business requires a registered business, an approved messaging provider, and conversations that the customer starts.
- **Reverse engineering isn't viable.**
  - pypush (the basis of Beeper Mini) was blocked by Apple within about two days in Dec 2023, and Apple said it would keep closing such loopholes ([Macwelt](https://www.macwelt.de/article/2167460/apple-stoppt-beeper-mini-imessages.html), [512pixels](https://512pixels.net/2023/12/beeper-is-just-about-done/)).
  - pypush's current rewrite supports APNs only; its README says iMessage support will come back "in future updates" ([pypush README](https://cdn.jsdelivr.net/gh/jjtech0130/pypush@main/README.md), [PyPI 2.0.0](https://pypi.org/project/pypush/2.0.0)).
  - **Not used.** It would break without warning and risks the Apple ID.
- **Hosted Macs.**
  - Scaleway Mac mini M2 costs €115/month ([Scaleway pricing](https://scaleway.com/en/pricing/apple-silicon)). No confirmation was found that iMessage sign-in is allowed there.
  - AWS EC2 Mac requires a 24 h minimum Dedicated Host ([AWS FAQ](https://aws.amazon.com/ec2/instance-types/mac/faqs/)). Third-party figures: mac-m4.metal about $1.23/h, roughly $900/month ([Green Mini comparison](https://www.greenmini.nl/compare/aws-ec2-mac-alternative/)) [unverified].
  - Both cost more than Blooio.
  - Beeper ran its own Mac mini fleet, one macOS user per customer ([Beeper](https://beeper.notion.site/iMessage-6444193636e14212844f3518dfaf7e40)). The same multi-user trick works for a shared family Mac.
- **Used Mac mini.** An M1 (8 GB/256 GB) costs roughly $300–470 used or refurbished in 2026 ([refurb.me](https://www.refurb.me/stats/mac-mini/mac-mini-m1-late-2020), [AppleInsider prices](https://prices.appleinsider.com/product/m1-mac-mini/mgnr3ll/a)). Idle draw is about 7 W, around $1/month.
- **Decision.** News247 ships its own relay (`news247 relay`; see [IMESSAGE-RELAY.md](../IMESSAGE-RELAY.md)).
  - It runs on any Mac the user controls and keeps an outbound WebSocket to the monitor.
  - The Mac's sleep or wake state is handled with a 5-second accept deadline, backup channels, and a late-delivery queue.

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
