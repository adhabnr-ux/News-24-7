# Your own iMessage gateway: the News247 relay

News247 can text you real iMessages (blue bubbles) **without Sendblue or any other paid service**. The monitor runs anywhere, for example a $7/month cloud server, and **any Mac signed in to Messages** does the sending. That Mac runs `news247 relay`, which is part of News247.

```
 News sources ──► News247 monitor (cloud, 24/7) ◄══ one outbound, authenticated WebSocket ══ Mac relay ──► Messages app ──► your iPhone
                    scores, decides, queues               (the Mac dials out; no open ports)        (news247 relay)    (iMessage)
                         │
                         └─► backup push (ntfy) when the Mac is asleep
```

## Why a Mac is unavoidable

Apple has **no public API for sending iMessages**. Every iMessage has to leave an Apple device signed in to an Apple ID:

- **Apple Messages for Business** is for registered companies working through an approved messaging provider, and the customer has to start the conversation. It doesn't fit personal alerts.
- **Reverse-engineered clients** (pypush, which powered Beeper Mini) were cut off by Apple within days in December 2023. pypush's current rewrite has dropped iMessage sending. Going this route breaks without warning and puts the Apple ID at risk.
- **Sendblue, Blooio, LoopMessage and similar services** run racks of Macs and resell access to them.

So "our own iMessage sender" means our own software running on a Mac you control. That is what the relay is. It does the same job as Sendblue's Mac fleet, with your Apple ID, for free, inside News247.

## What you get

| | |
|---|---|
| **Speed** | The alert goes to the Mac over an open connection the moment News247 decides to send it. Messages hands it to Apple in under a second. The dashboard shows the median time until **your iPhone confirmed delivery**. |
| **Delivery receipts** | The relay reads the Messages database and reports *delivered* or *not delivered* for every text. With `--service auto`, a failed iMessage is retried as SMS. |
| **Mac asleep or off** | The monitor gives a relay 5 s to accept a message. If it doesn't, the alert goes to your **backup channel** (e.g. ntfy push) immediately. If there's no backup, it's queued. When the Mac reconnects you get what you missed: one text each with "⏱ 12m late", or **one digest** if 3+ piled up. Anything older than 60 min is dropped. Alerts a backup already delivered are never repeated. |
| **Text it commands** | Reply to any alert: `PAUSE 2h`, `STOP`, `RESUME`, `CRITICAL` (only the biggest), `MORE`, `NORMAL`, `STATUS`, `HELP`. |
| **Security** | The Mac dials out, so there are no open ports or tunnels. Both sides prove they know a shared secret without sending it (HMAC challenge-response). Every message is signed and numbered, so it can't be forged or replayed. A relay pointed at the wrong server refuses to send anything. An optional `--allow +1…` list means even a compromised server can't make your Mac text anyone else. A built-in rate limit (40 texts / 10 min) protects the Apple ID. |
| **Several Macs** | Run the relay on two Macs (e.g. a Mac mini and a laptop). The monitor uses whichever is awake and fails over between them. |

## Which Mac?

| Option | Cost | 24/7? | Notes |
|---|---|---|---|
| **Your own Mac** | free | only while awake | Fine with an **ntfy backup**: iMessage while the Mac is on, a push notification while it sleeps. Use a separate Apple ID in Messages, or a second macOS user account (below). |
| **A used Mac mini that stays plugged in** | ~$300–470 one-off for an M1 (2026 prices); ~$1/month of power (about 7 W idle) | **yes** | The real "own Sendblue". Any Mac that runs macOS 11+ works; older Intel minis cost less. |
| **A family member's / office Mac** | free | if it stays on | Create a separate macOS user for the relay. With fast user switching it keeps running in the background. |
| Rented cloud Mac (Scaleway M2 €115/month; AWS EC2 Mac has a 24 h minimum at roughly $0.65–1.25/hour) | €115–900/month | yes | More expensive than Blooio ($39/month), and whether you may sign in to iMessage there is unconfirmed. Not recommended. |

Can your **iPhone** be the relay? No. iOS doesn't let a background app send iMessages, and a server can't trigger a Shortcuts automation instantly unless a third-party automation app is kept open in the foreground (realistic only on a *spare* iPhone or iPad). News247 doesn't support that.

## Set it up (5 minutes)

1. **On the server**, turn the relay on: `RELAY_ENABLED=true` (the Render blueprint already does this) and set your number in `IMESSAGE_TO` or on the Setup page. `RELAY_SECRET` is optional: if it's empty, News247 generates one and stores it.
2. **Open the dashboard's Setup page** (`https://your-monitor/setup?token=…`). Under **Your iMessage relay**, copy the one-line command.
3. **On the Mac**, open Terminal and paste it. The installer:
   - finds Python 3.10+, or fetches 3.12 with `uv` (macOS's own 3.9 is too old),
   - downloads News247 *from your monitor* (same version, no git or GitHub needed),
   - saves the server address and secret to a private file (`chmod 600`),
   - installs a LaunchAgent that starts at login, restarts on crash and stops the Mac from idle-sleeping,
   - runs `news247 relay doctor`, which checks everything below.
4. When macOS asks whether **python may control Messages**, click **OK**.
5. *(Recommended)* Grant **Full Disk Access** to the Python path the installer prints. It's copied to your clipboard and the settings pane opens for you. Without it, alerts still send, but there are no delivery receipts and no text commands.
6. Press **Send test message** on the Setup page.

Without the dashboard, from a clone: `./deploy/install-relay-macos.sh --server https://your-monitor --secret <RELAY_SECRET>`.

### Make the alerts *ring*
- **Use a separate Apple ID on the relay Mac.** Messages your own Apple ID sends to your own number land in a "note to self" thread **with no notification**. Either:
  - make a free Apple ID (e.g. `news247.alerts@icloud.com`) and sign Messages on the relay Mac into it, or
  - create a second macOS user for the relay, so your own Messages stays untouched.

  The relay warns on the Setup page when it detects that it's texting its own Apple ID.
- **Save that Apple ID as a contact on your iPhone** ("News247"). iOS's *Filter Unknown Senders* would otherwise file alerts silently under "Unknown Senders".
- Optional: give the contact an **Emergency Bypass** ringtone so CRITICAL alerts get through Focus / Do Not Disturb.

## Run it by hand

```bash
news247 relay --server https://your-monitor --secret <RELAY_SECRET>      # foreground, Ctrl-C to stop
news247 relay doctor --server … --secret …                                # checklist with fixes
news247 relay send-test +15551234567                                      # one message, waits for the receipt
news247 relay --allow +15551234567 --service auto …                       # only ever text this number; SMS fallback
```

Installed service:

| | |
|---|---|
| Logs | `~/Library/Logs/News247Relay.log` |
| Restart | `launchctl kickstart -k gui/$(id -u)/com.news247.relay` |
| Settings | `~/Library/Application Support/News247Relay/relay.env` |
| Uninstall | re-run the install command with `bash -s -- --uninstall` at the end |

## Server settings (`notify.relay` in config.yaml)

| Option | Default | Meaning |
|---|---|---|
| `enabled` | `${RELAY_ENABLED:-false}` | |
| `to` | `${IMESSAGE_TO}` | Recipients. The Setup page overrides this. |
| `secret` | `${RELAY_SECRET}` | Empty = generated once, stored in the database, shown on the Setup page. |
| `accept_timeout` | `5` | Seconds a relay has to accept a message before it counts as asleep and backups take over. |
| `result_timeout` | `45` | Seconds to wait for Messages to finish sending. |
| `late_delivery` | `true` | Deliver queued alerts when a relay reconnects. |
| `max_late_minutes` | `60` | Older queued alerts are dropped instead. |
| `digest_min` | `3` | This many queued alerts arrive as one digest text. |

Backups: put `backup: true` on any channel. A backup channel only fires when **every** primary phone channel failed for that alert. If it's the only phone channel, it behaves like a normal channel. Example for free push while the Mac sleeps: `NTFY_ENABLED=true NTFY_BACKUP=true NTFY_TOPIC=<secret topic>`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Setup page: "No Mac connected" | On the Mac: `tail ~/Library/Logs/News247Relay.log`. "rejected this relay's secret" → copy the install command again (the secret changed). "cannot reach" → check the URL and the Mac's internet. |
| "did not answer within 5s (asleep?)" | The Mac slept. Keep it plugged in. For laptops: System Settings > Battery > Options > *Prevent automatic sleeping when the display is off*. A closed lid still sleeps unless an external display is attached. |
| "Not authorized to send Apple events" (-1743) | System Settings > Privacy & Security > Automation > allow python → Messages. |
| Delivered, but no notification | You're texting the relay Mac's own Apple ID: see *Make the alerts ring*. |
| Recent texts show `failed` (Messages' red "Not Delivered") | The number may not be reachable by iMessage. Check it, or use `--service auto` (the SMS fallback needs an iPhone on the relay's Apple ID with Text Message Forwarding to this Mac). |
| "receipts off" on the Setup page | Grant Full Disk Access to the Python path in the install output, then restart the relay. |

## Protocol (for the curious)

The relay opens `wss://<monitor>/relay/ws`. Everything is JSON text frames.

1. Hub → `{"type":"challenge","v":1,"nonce":Ns,"time":…}`
2. Relay → `{"type":"hello","nonce":Na,"proof":HMAC(secret,"hello|Ns|Na"),"info":{name, macOS, version, receipts…}}`
3. Hub verifies → `{"type":"welcome","proof":HMAC(secret,"welcome|Na|Ns")}`. The relay verifies this too. A wrong secret closes the socket with code 4001.
4. From then on, every frame carries `seq` (strictly increasing) and `mac` = HMAC-SHA256(session key, canonical JSON), where session key = HMAC(secret, "news247-relay-session|Ns|Na").
   - Hub → relay: `config {recipients, accept_timeout}`, `send {id, to, text, severity, created, issued}`
   - Relay → hub: `accepted {id}`, then `result {id, ok, error, per[]}`, later `receipt {id, status: delivered|failed|sent}`; plus `inbound {from, text}` (your reply) and `status {info}` every minute.

`accepted` is sent before Messages is touched, which is how the hub tells "busy" from "asleep" within 5 s. The relay drops any `send` it reads more than `accept_timeout + 2 s` after it was issued: the Mac was asleep and the hub has already passed the alert to a backup or the queue. Message ids are remembered on disk, so a reconnect never double-texts.

Code: [`news247/relay/`](../news247/relay/). Tests: [`tests/test_relay.py`](../tests/test_relay.py). They run the hub and a relay over a real WebSocket and cover authentication both ways, queueing, digests, expiry, backups, sleeping relays, receipts, text commands, the installer and the package download.
