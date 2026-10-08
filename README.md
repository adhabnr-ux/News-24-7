# News 24/7: a market-moving news monitor

News247 runs around the clock and pings your phone within seconds when something breaks that is likely to move stocks. Examples: OpenAI launching an enterprise agent that hits software stocks, an 8-K bankruptcy filing, a "news pending" trading halt, a Fed statement, a tariff post, or a whole sector suddenly dropping 3%.

It watches about 50 sources at once: AI-lab and big-tech newsrooms, newspaper scoop feeds (FT, Bloomberg, The Information), headline squawks, SEC filings, exchange halts, the Fed and White House, press-release wires, financial media, and Bluesky in real time, plus optional push feeds (X, Alpaca/Benzinga). It also watches **prices**, so if software stocks fall together it tells you that, and names the headline that most likely caused it. Alerts arrive as **iMessages to your phone number**, or through ntfy, Telegram, Discord, Slack, Pushover, email or SMS.

![dashboard](docs/dashboard.png)

```
🔴 CRITICAL  @DeItaone: *OPENAI LAUNCHES ENTERPRISE AGENTS FOR FINANCE, LEGAL AND SALES
    x (primary) · published 3s ago, caught 2s after publish
    Watch: CRM NOW ADBE INTU WDAY TEAM HUBS MNDY DOCU TRI  ▼ down
    Themes: ai lab launch, ai disrupts software
    Confirmed by 2 sources · ⬆ escalated

🔴 CRITICAL  ▼ SOFTWARE stocks -4.12% in 10m (15/15 moving together)
    MNDY -4.92%, HUBS -4.92%, TEAM -4.67%, DOCU -4.67%, WDAY -4.42% …
    Possible catalyst (7s earlier): @DeItaone: *OPENAI LAUNCHES ENTERPRISE AGENTS … [x]
    Possible catalyst (7s earlier): Introducing ChatGPT Agents for finance, legal and sales teams [openai-news]
```

---

## Quick start: iMessage alerts, running 24/7 on a Mac (5 minutes)

```bash
git clone https://github.com/adhabnr-ux/News-24-7.git && cd News-24-7
./deploy/install-macos.sh
```

The installer:
1. installs everything (needs Python 3.10+; `brew install python@3.12` if you don't have it);
2. asks for **your phone number** (alerts go there as iMessages) and an e-mail (the SEC requires a contact in requests);
3. optionally asks for an X API token and free Alpaca keys (the fastest sources — see [How fast is it?](#how-fast-is-it));
4. installs a background service that starts at login, restarts itself if it crashes, and keeps the Mac awake;
5. sends you a **test iMessage**. macOS asks once whether Terminal may control Messages: click **OK**.

That's it. The dashboard is at http://localhost:8247. Logs are in `data/news247.log`.

**Requirements for iMessage:** the Mac stays on, plugged in, and signed in to Messages (Messages ▸ Settings ▸ iMessage). A laptop with its lid closed sleeps unless it's connected to an external display.

**Tip:** sign Messages on that Mac into a **separate Apple ID** (a free "bot" account) rather than your own. Alerts then arrive on your iPhone as normal incoming iMessages with a notification sound, and you can give that contact a custom tone. Messages you send to yourself may not notify.

To see it working before setting anything up, run `.venv/bin/news247 demo`. It plays a simulated "OpenAI launch → software selloff" through the real pipeline.

### No Mac, or want it running in the cloud?

| Option | Bubble | Cost | Setup |
|---|---|---|---|
| **BlueBubbles** relay | blue (iMessage) | free | Monitor runs anywhere (VPS, Linux, Docker); a Mac at home runs the free [BlueBubbles](https://bluebubbles.app) server. Set `BLUEBUBBLES_ENABLED`, `BLUEBUBBLES_URL`, `BLUEBUBBLES_PASSWORD`, `IMESSAGE_TO`. |
| **Sendblue** | blue (iMessage) | free sandbox (10 contacts); paid from ~$29/mo | No Mac at all. Text your Sendblue number once from your phone, then set `SENDBLUE_ENABLED`, `SENDBLUE_API_KEY_ID`, `SENDBLUE_API_SECRET`, `SENDBLUE_FROM`, `IMESSAGE_TO`. |
| **Blooio** | blue (iMessage) | from ~$39/mo | No Mac. `BLOOIO_ENABLED`, `BLOOIO_API_KEY`, `IMESSAGE_TO`. |
| **Textbelt** | green (SMS) | prepaid, ~a few cents/text | No registration paperwork. Links are stripped (Textbelt holds link texts until your account is verified). `TEXTBELT_ENABLED`, `TEXTBELT_KEY`, `SMS_TO`. |
| **Twilio** | green (SMS) | ~$1/mo + ~1¢/text | US carriers require A2P 10DLC or toll-free verification first (≈$45 and 1–3 weeks for a sole proprietor). |
| **ntfy** app | push notification | free | Install **ntfy**, subscribe to a secret topic, set `NTFY_TOPIC` and `notify.ntfy.enabled: true`. Most reliable fallback; consider enabling it alongside iMessage. |
| Telegram / Discord / Slack / Pushover / e-mail / webhook | | free | See `config.example.yaml`. |

All of these are switched on from `.env` (see `news247 init`). Check any channel with `news247 test-notify --only imessage` (or `sendblue`, `ntfy`, …).

Each channel has a `min_severity`. By default your phone gets **HIGH + CRITICAL**, the console also shows **MEDIUM**, and quiet hours (e.g. 23:30–06:30) let only **CRITICAL** through.

### Manual install (Linux, Windows, Docker)

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .
news247 init        # writes config.yaml + .env; fill in .env
news247 check       # tests every source from YOUR network and shows what works
news247 run         # start monitoring; dashboard at http://localhost:8247
```

---

## How fast is it?

### Lesson from 2026-10-08: the OpenAI news that moved stocks wasn't an OpenAI post

Around 1 pm ET on 2026-10-08, the **Financial Times** reported that OpenAI had told investors its annualized revenue was about **$50B**, against the ~$70B investors had assumed. Bloomberg relayed it at 1:04 pm, and squawk accounts posted it within seconds. The Nasdaq-100 fell more than 300 points in about 30 minutes: NVDA −3%, ORCL −6%, CRWV −8%, AMD and AVGO −5%. Yahoo and Google News had the story **30+ minutes later**, after the move.

So News247 watches two lanes:

1. **First-party posts.** The labs' own sites, YouTube, and X accounts. These are **VIP sources**: anything OpenAI or Anthropic publishes is texted to you instantly, whatever it says. It never waits for confirmation or for the AI model.
2. **Scoops.** Newspapers break most market-moving AI news (revenue, funding, chip deals). The fastest ways to catch them are squawk accounts (@DeItaone "Walter Bloomberg", @FirstSquawk, FinancialJuice), the scoop publishers' own feeds (FT, Bloomberg, The Information), and newsdesk wires (Benzinga via Alpaca). The scorer knows that AI-lab money news re-prices the whole AI trade (`ai_lab_financials` → NVDA, ORCL, CRWV, AMD, AVGO, MSFT, SMCI) and that "FT says", "told investors" and "people familiar" mark a scoop.

That exact event is replayed in the test suite (`tests/test_fast_sources.py::test_replay_openai_revenue_scoop`): the squawk is texted on arrival, the Bloomberg version a minute later merges into the same story instead of sending a second text, and the selloff alert names the scoop as the cause.

### Sources by speed

| Source | How | Typical time to your phone | Cost |
|---|---|---|---|
| **X stream**: @OpenAI, @sama, @AnthropicAI… (VIP) + @DeItaone, @FirstSquawk, @financialjuice… | **push** (official filtered stream) | ~2–5 s | pay-per-use, ~$0.005/post (~$10–60/mo depending on accounts) |
| **Alpaca news** (Benzinga newsdesk, tickers attached) | **push** (websocket) | seconds–2 min | free (paper-trading keys) |
| Bluesky newsrooms (Reuters, AP, WSJ, Bloomberg, NYT…) | **push** (Jetstream) | ~1–2 s after they post | free |
| Telegram squawks (FinancialJuice…) | poll every 5 s | ~5–10 s | free |
| OpenAI news RSS, Anthropic newsroom (VIP) | poll every 10 s | ~10 s + the site's own feed lag | free |
| OpenAI YouTube (livestream announcements, VIP) | poll every 30 s | ~30 s + YouTube feed lag | free |
| FT, Bloomberg Tech, The Information, Axios feeds | poll every 20–30 s | ~20 s + feed lag (usually 1–5 min after the article) | free |
| SEC EDGAR 8-K filings | poll every 10 s | ~10–30 s after EDGAR posts it | free |
| Nasdaq trading halts (all US exchanges) | poll every 15 s | ~15–30 s | free |
| Fed, White House, Treasury, BLS, FDA, ECB… | poll every 15–60 s | ~15–60 s | free |
| Press-release wires, MarketWatch bulletins, CNBC, WSJ | poll every 20–45 s | ~30–90 s | free |
| Prices: Yahoo (free) / Finnhub (free key, push) | poll 15 s / push | ~15–30 s / ~1–3 s | free |
| Hacker News, Google News | poll 30–90 s | minutes (used as confirmation only) | free |

**What "instantly" honestly takes:** the free lanes already beat the aggregators by a wide margin. To be told within seconds of a scoop or an executive's post, enable the **X stream** (the single biggest upgrade) and the free **Alpaca** news stream. The installer asks for both, or you can set them in `.env` later. True sub-second institutional feeds (Bloomberg Terminal, Dow Jones/LSEG direct, Truth Social's official API) cost $25k–$100k+ a year and are out of scope.

Built-in safeguards: polling uses conditional GETs, so checking every 5–10 s costs almost nothing. Squawk relays and publisher copies of the same story are merged into one text. Posts from a VIP account that look like a crypto/airdrop scam (OpenAI's newsroom account was hijacked in 2024) do not get the instant treatment.

`news247 stats` shows, for your own machine, each source's publish→detect lag and **which source had each story first**, so you can see which feeds are worth keeping.

---

## How it decides what matters

Every item is scored from 0 to 100 by a deterministic rule engine. It takes about 50 µs per item, never goes down, and every score can be explained:

- **Source tier**: primary (the company/regulator itself) > wire > media > social
- **Event keywords**: bankruptcy, to acquire, cuts guidance, FDA rejects, export ban, rate cut, tariff, halt, …. The best match counts fully, the next ones count less.
- **Themes**, i.e. multi-condition patterns that encode how markets react:
  - `ai_disrupts_software`: an AI lab *launches* an *agent/plugin for legal/finance/sales/coding/…* → CRM, NOW, ADBE, INTU, WDAY… **down**
  - `ai_compute_deal`: a giant data-center or GPU deal → NVDA, AMD, AVGO, ORCL, VRT… **up**
  - `cheap_frontier_model`: a DeepSeek-style cheap model → NVDA, AVGO, TSM… **down**
  - `ai_lab_financials`: OpenAI/Anthropic/xAI revenue, valuation, funding or spending news → NVDA, ORCL, CRWV, AMD, AVGO, MSFT… (the 2026-10-08 selloff)
  - also `chip_export_controls`, `fed_policy`, `trade_war`, `macro_print`, `bigtech_antitrust`, `crypto_policy`, `geopolitical_shock`, `ai_disrupts_search`, `ai_lab_launch`
- **Companies**: 80+ companies and macro actors with aliases. Private AI labs map to the public stocks *exposed* to them (OpenAI → MSFT, NVDA, ORCL, AMD, AVGO, CRWV). A post on OpenAI's own blog counts as an OpenAI announcement even if the headline is just "Introducing X".
- **Penalties**: law-firm class-action spam, "stocks to buy", "here's why", event schedules, podcasts, question headlines
- **Confirmation**: the same story from several outlets is clustered into **one** alert. Each extra source raises the score, and an alert re-fires only when the story *escalates* to a higher severity.
- **Signals from the source itself**: 8-K item codes (1.03 bankruptcy, 4.02 restatement…), halt reason codes (T1 news pending, MWC circuit breaker)

Severity: **CRITICAL ≥ 75**, **HIGH ≥ 55**, **MEDIUM ≥ 35** (all configurable).

Tune it with `news247 score`:

```text
$ news247 score "Introducing GPT-6" --tier primary --entity OpenAI

  🟠 HIGH  score 62.0/100   direction: unknown
  tickers:  MSFT NVDA ORCL AMD AVGO CRWV
  entities: OpenAI
  themes:   ai_lab_launch

    +20 primary source
    +18 keyword 'gpt-6*'
    +4 keyword 'introduc*'
    +5 known entity (OpenAI)
    +15 theme ai_lab_launch
```

### Optional: local AI second opinion (Ollama)

```bash
# install from https://ollama.com, then
ollama pull qwen2.5:7b-instruct
```

```yaml
llm:
  enabled: true
  model: qwen2.5:7b-instruct
```

Items the rules find at least somewhat interesting are sent to your local model. It rates impact from 0 to 10, names the affected tickers, and writes a one-line "why it matters" that appears in the alert. Design choices:

- **CRITICAL alerts never wait for the model.** HIGH alerts wait at most `budget_ms` (2.5 s). If the model finishes later and raises the severity, you get an escalation.
- The model can only **nudge** the score (−20 / +25), so a confused model cannot hide a circuit breaker.
- The model is kept loaded in memory (`keep_alive`) to avoid cold-start delays.
- Any OpenAI-compatible server works too (LM Studio, llama.cpp, vLLM): `provider: openai`.

---

## Price-move detection: "all the stocks are crashing"

- **Single stocks**: ±1.5 % in 1 min, ±3 % in 5 min, ±5 % in 15 min (index ETFs have tighter built-in limits, e.g. SPY ±0.8 % in 5 min). Also one alert per day for a ±7 % move from the previous close.
- **Sector baskets**: software, semis, megacap, ai_infra, crypto and index. An alert fires when the *average* move crosses the threshold **and** enough members move together, so one stock crashing doesn't count as a sector move.
- **Coalescing**: if 15 software stocks drop at once you get **one** alert, not 15.
- **Correlation**: every price alert lists the recent headlines (within 45 min) that mention the stocks that moved: *"Possible catalyst (2m earlier): …"*. News alerts show the current move of the affected tickers.
- Cooldowns stop repeat alerts. A move that becomes 1.5× worse alerts again.

Data: **Yahoo** (free, polled, includes pre/after-hours) or **Finnhub** (free key, real-time websocket for about 50 symbols).

---

## Run it 24/7

It needs a machine that stays on: a small cloud VPS ($4–6/month), a Raspberry Pi, a home server, or a desktop that doesn't sleep.

**Docker** (recommended):
```bash
news247 init               # or copy news247/data/config.example.yaml to config.yaml
docker compose up -d       # restarts automatically; dashboard on http://localhost:8247
docker compose logs -f
# with a local LLM:
docker compose --profile ai up -d && docker compose exec ollama ollama pull qwen2.5:7b-instruct
#   and set llm.base_url: http://ollama:11434
```

**macOS**: `./deploy/install-macos.sh` (see Quick start; `--uninstall` removes it). **Linux (systemd)**: `deploy/news247.service`. **Windows**: `deploy/windows-task.ps1`. Each file has install instructions at the top.

Built in to keep it running:
- Every source runs independently. A failing source backs off exponentially, honours `Retry-After`, and never affects the others.
- Websockets reconnect automatically and resume from the last cursor.
- Seen items are kept in SQLite, so a restart never re-sends alerts. On startup, only items published in the last 5 minutes are pushed; older ones go to the dashboard only.
- If **no** source has succeeded for 5 minutes (e.g. the internet is down), you get one alert, and another when it recovers.
- `/health` endpoint, Docker healthcheck, rotating log in `data/news247.log`.
- To open the dashboard from your phone on your LAN: `web.host: 0.0.0.0` **plus** `web.token`.

---

## Optional upgrades

| Upgrade | What you gain | How |
|---|---|---|
| **X stream** | First-party posts (OpenAI, sama, Anthropic, Google DeepMind, xAI, Nvidia…) and squawk relays of FT/WSJ/Bloomberg scoops within seconds | Token at developer.x.com (pay-per-use). In `.env`: `X_BEARER_TOKEN=…`, `X_STREAM_ENABLED=true`. Edit the accounts under `x-stream` in config. |
| **Alpaca news** (free) | Benzinga newsdesk headlines pushed in real time, with tickers | Free account at app.alpaca.markets → API keys. `ALPACA_KEY`, `ALPACA_SECRET`, `ALPACA_ENABLED=true`. |
| **Finnhub** (free key) | Real-time trades instead of 15-s price polling | `market.provider: finnhub`, `FINNHUB_TOKEN` |
| **Truth Social** | Presidential posts that move tariffs/markets | Cloudflare blocks datacenter IPs: run from home and enable `truth-social`, or rely on the `trump-truth-mirror` feed (third-party; confirm with `news247 check`) |
| **Reddit** | WSB/stocks chatter (free OAuth "script" app) | `reddit` source + `REDDIT_CLIENT_ID/SECRET` |
| **Local AI** (Ollama) | A one-line "why it matters" in each text, and a second opinion on borderline items | See [Optional: local AI second opinion](#optional-local-ai-second-opinion-ollama) |

Add any other site in two lines:

```yaml
sources:
  - {name: my-feed, type: rss, url: "https://example.com/feed.xml", tier: media, interval: 30}
  - {name: xai-newsroom, type: pagewatch, url: "https://x.ai/news", link_pattern: 'x\.ai/news/[a-z0-9-]+$', entities: [xAI]}
```

`pagewatch` monitors any page that has no RSS feed. It reports each new link that matches the pattern as a new post (the built-in Anthropic and Treasury sources use it).

---

## Configuration

Everything lives in `config.yaml` (from `news247 init`). Every option is documented in [`news247/data/config.example.yaml`](news247/data/config.example.yaml). The main sections:

- `scoring`: thresholds, your `watchlist`, extra `keywords` / `companies` / `themes`, `mute` patterns
- `market`: provider, symbols, move `rules`, per-symbol `overrides`, sector `groups`
- `sources`: override built-ins by name (`enabled: false`, `interval: 10`, …) or add new ones. The built-ins are in [`news247/data/default_sources.yaml`](news247/data/default_sources.yaml).
- `notify`: channels, `min_severity` per channel, `quiet_hours`, `rate_limit_per_minute`
- `llm`, `web`, `general`

Secrets go in `.env` and are referenced from the config as `${NAME}`.

### Commands

| Command | What it does |
|---|---|
| `news247 run` | Start monitoring (`--no-web`, `--no-market`, `--port`, `--host`) |
| `news247 check [names…]` | Fetch every source once from this machine; shows status, newest item and errors |
| `news247 score "headline"` | Explain a score (`--tier primary --entity OpenAI --summary …`) |
| `news247 test-notify [--only imessage]` | Send a test alert through every enabled channel (or just one) |
| `news247 demo` | Simulated "AI launch → software selloff" through the real pipeline, dashboard and notifications |
| `news247 stats` | Measured detection latency per source, and which source had each story first |
| `news247 init` | Write a starter `config.yaml` + `.env` |

### API

`GET /api/alerts`, `/api/items?min_score=50`, `/api/market`, `/api/status`, `/api/latency`, `/health`, and `GET /events` (Server-Sent Events stream of `item` / `alert` / `item_update`).

---

## Architecture

```
 sources (async, independent)                        pipeline                              outputs
 ───────────────────────────                         ────────                              ───────
 push: X stream · Alpaca · Bluesky ───┐
 RSS/Atom · SEC EDGAR · Nasdaq halts ─┤                     VIP floor (labs: instant)
 page watcher · Telegram · HN · Reddit┼─► dedupe ─► score (rules) ─► cluster stories ─┬─► SQLite (history, seen ids)
 Mastodon/Truth Social ───────────────┘   (uid)     tickers/themes   +confirmation    ├─► dashboard (SSE) + JSON API
                                                          │                           └─► alert ─► iMessage/BlueBubbles/
                                          optional local LLM (bounded, time-boxed)              Sendblue/SMS/ntfy/Telegram/
                                                                                                Discord/Slack/email/webhook
 Yahoo / Finnhub prices ─► move detector ─► coalesce ─► correlate with recent news ─► price alert
                           (rules, baskets,   (one alert per
                            cooldowns)          sector move)
```

```
news247/
  sources/      rss, sec_edgar, halts, pagewatch, bluesky, x_stream, alpaca_news, telegram, social (x, mastodon),
                apis (hn, reddit, finnhub)
  analysis/     scorer, entities, dedup (story clustering), llm
  market/       detector (move rules, baskets), prices (yahoo, finnhub)
  notify/       channels (iMessage, BlueBubbles, Sendblue, Blooio, SMS, ntfy, Telegram, …) + dispatcher
  web/          dashboard + API
  data/         knowledge.yaml (companies, keywords, themes, baskets), default_sources.yaml, config.example.yaml
  engine.py     the pipeline · storage.py SQLite · cli.py
```

## Development

```bash
pip install -e ".[dev]"
pytest -q          # 150+ tests: parsers on real feed formats, scoring calibration, move detection,
                   # every notification channel's wire format, LLM client, engine end-to-end, web API
ruff check . && ruff format --check .
```

---

*For information only, not financial advice. Respect each source's terms of use and rate limits: the SEC asks for a descriptive User-Agent and at most 10 requests/second, which the HTTP client enforces for you.*
