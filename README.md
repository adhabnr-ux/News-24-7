# News 24/7: a market-moving news monitor

News247 runs around the clock and pings your phone within seconds when something breaks that is likely to move stocks. Examples: OpenAI launching an enterprise agent that hits software stocks, an 8-K bankruptcy filing, a "news pending" trading halt, a Fed statement, a tariff post, or a whole sector suddenly dropping 3%.

It watches about 40 free sources at once: company newsrooms, SEC filings, exchange halts, the Fed and White House, press-release wires, financial media, Bluesky in real time, and optionally X, Reddit and Truth Social. It also watches **prices**, so if software stocks fall together it tells you that, and names the headline that most likely caused it.

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

## Quick start (5 minutes)

Requires Python 3.10+.

```bash
git clone https://github.com/adhabnr-ux/News-24-7.git && cd News-24-7
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -e .

news247 demo        # see the whole thing work on a simulated "OpenAI launch → software selloff"
                    # → open http://localhost:8247

news247 init        # writes config.yaml + .env
#   1. set general.user_agent to "YourName you@email.com" (the SEC requires a contact)
#   2. set up phone alerts (below)
news247 check       # tests every source from YOUR network and shows what works
news247 run         # start monitoring; dashboard at http://localhost:8247
```

With no config file at all, `news247 run` still works. It uses the built-in sources, prints alerts to the console and serves the dashboard.

### Get alerts on your phone (pick one)

| Channel | Setup | Notes |
|---|---|---|
| **ntfy** (recommended) | Install the free **ntfy** app (iOS/Android) → subscribe to a hard-to-guess topic such as `adhab-markets-x7k2` → put `NTFY_TOPIC=adhab-markets-x7k2` in `.env` and set `notify.ntfy.enabled: true` | No account needed. CRITICAL alerts use max priority, so they break through Do Not Disturb if you allow it. |
| **Telegram** | Message **@BotFather** → `/newbot` → copy the token; message your bot once; open `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat.id` | Set `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` |
| **Discord / Slack** | Channel settings → Integrations → Webhook → copy URL | `@here` ping on CRITICAL |
| **Pushover** | App token + user key | Siren sound on CRITICAL |
| **Email** | SMTP (for Gmail, use an app password) | Default: CRITICAL only |
| **Desktop pop-ups** | `notify.desktop.enabled: true` | macOS, Linux (`notify-send`), Windows |
| **Webhook** | Any URL | Full alert JSON, for n8n, Zapier, Home Assistant or your own trading bot |

Then run `news247 test-notify`. It sends a test alert through every enabled channel.

Each channel has a `min_severity`. The defaults: phone gets **HIGH + CRITICAL**, the console also shows **MEDIUM**, and quiet hours (e.g. 23:30–06:30) let only **CRITICAL** through.

---

## How fast is it?

Detection time = how often a source is polled + how long the publisher takes to put the post in its own feed. Polling uses conditional GET (ETag/Last-Modified), so checking every 10–20 s costs almost nothing.

| Source | How | Typical time to your phone |
|---|---|---|
| Bluesky accounts (Reuters, AP, WSJ, Bloomberg, CNBC, … — handles that don't exist are skipped) | **push** (Jetstream websocket) | ~1–2 s |
| Price moves (Finnhub, free key) | **push** (trade websocket) | ~1–3 s |
| X/Twitter accounts (OpenAI, sama, DeItaone, FirstSquawk, …) *(paid API)* | 1 query covers all accounts, every 20 s | ~5–20 s |
| SEC EDGAR 8-K filings | poll every 10 s | ~10–30 s after EDGAR posts it |
| Nasdaq trading halts (all US exchanges) | poll every 15 s | ~15–30 s |
| Fed, White House, Treasury, BLS, FDA, ECB… | poll every 15–60 s | ~15–60 s |
| OpenAI newsroom (RSS), Anthropic newsroom (page watcher) | poll every 20 s | ~20 s + feed lag |
| PR Newswire / GlobeNewswire / Business Wire | poll every 20 s | ~20–60 s |
| MarketWatch bulletins, CNBC, WSJ, Bloomberg | poll every 20–45 s | ~30–90 s |
| Price moves (Yahoo, free, no key) | poll every 15 s | ~15–30 s |
| Hacker News front page, Google News | poll 30–90 s | minutes (used as confirmation) |

`news247 stats` shows the measured publish→detect lag for each source on your machine.

**About "the moment it posts":** no free source delivers Bloomberg-terminal speed. The fastest free paths are built in: Bluesky push, SEC/halts polling, primary newsrooms, and the price-move detector, which often fires *before* any headline exists. The paid upgrades in "Making it even faster" below close most of the remaining gap.

---

## How it decides what matters

Every item is scored from 0 to 100 by a deterministic rule engine. It takes about 50 µs per item, never goes down, and every score can be explained:

- **Source tier**: primary (the company/regulator itself) > wire > media > social
- **Event keywords**: bankruptcy, to acquire, cuts guidance, FDA rejects, export ban, rate cut, tariff, halt, …. The best match counts fully, the next ones count less.
- **Themes**, i.e. multi-condition patterns that encode how markets react:
  - `ai_disrupts_software`: an AI lab *launches* an *agent/plugin for legal/finance/sales/coding/…* → CRM, NOW, ADBE, INTU, WDAY… **down**
  - `ai_compute_deal`: a giant data-center or GPU deal → NVDA, AMD, AVGO, ORCL, VRT… **up**
  - `cheap_frontier_model`: a DeepSeek-style cheap model → NVDA, AVGO, TSM… **down**
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

**Linux (systemd)**: `deploy/news247.service`. **macOS (launchd)**: `deploy/com.news247.monitor.plist`. **Windows**: `deploy/windows-task.ps1`. Each file has install instructions at the top.

Built in to keep it running:
- Every source runs independently. A failing source backs off exponentially, honours `Retry-After`, and never affects the others.
- Websockets reconnect automatically and resume from the last cursor.
- Seen items are kept in SQLite, so a restart never re-sends alerts. On startup, only items published in the last 5 minutes are pushed; older ones go to the dashboard only.
- If **no** source has succeeded for 5 minutes (e.g. the internet is down), you get one alert, and another when it recovers.
- `/health` endpoint, Docker healthcheck, rotating log in `data/news247.log`.
- To open the dashboard from your phone on your LAN: `web.host: 0.0.0.0` **plus** `web.token`.

---

## Making it even faster (optional paid upgrades)

| Upgrade | What you gain | How |
|---|---|---|
| **X API** (Basic tier) | Executives' and headline accounts' posts (sama, OpenAI, DeItaone, FirstSquawk…) within seconds | `sources: - {name: x, enabled: true}` + `X_BEARER_TOKEN` |
| **Finnhub** (free key) | Real-time trades instead of 15-s polling, plus a market-news feed | `market.provider: finnhub`, `FINNHUB_TOKEN`; enable `finnhub-news` |
| **Truth Social** | Presidential posts that move tariffs or markets | Cloudflare blocks datacenter IPs. Run from home and enable `truth-social`, or rely on the built-in `trump-truth-mirror` feed (a third-party mirror; confirm it with `news247 check`) |
| **Reddit** | WSB/stocks chatter (needs a free OAuth "script" app since 2026) | `reddit` source + `REDDIT_CLIENT_ID/SECRET` |
| Paid newswires (Benzinga Pro, etc.) | Squawk-level speed | Add as `rss`/`webhook` sources, or ask for a dedicated adapter |

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
| `news247 test-notify` | Send a test alert through every enabled channel |
| `news247 demo` | Simulated "AI launch → software selloff" through the real pipeline, dashboard and notifications |
| `news247 stats` | Measured detection latency per source |
| `news247 init` | Write a starter `config.yaml` + `.env` |

### API

`GET /api/alerts`, `/api/items?min_score=50`, `/api/market`, `/api/status`, `/api/latency`, `/health`, and `GET /events` (Server-Sent Events stream of `item` / `alert` / `item_update`).

---

## Architecture

```
 sources (async, independent)                        pipeline                              outputs
 ───────────────────────────                         ────────                              ───────
 RSS/Atom · SEC EDGAR · Nasdaq halts ─┐
 page watcher · HN · Reddit · X ──────┼─► dedupe ─► score (rules) ─► cluster stories ─┬─► SQLite (history, seen ids)
 Mastodon/Truth · Bluesky (websocket)─┘   (uid)     tickers/themes   +confirmation    ├─► dashboard (SSE) + JSON API
                                                          │                           └─► alert ─► ntfy/Telegram/Discord/
                                          optional local LLM (bounded, time-boxed)              Slack/Pushover/email/
                                                                                                desktop/webhook
 Yahoo / Finnhub prices ─► move detector ─► coalesce ─► correlate with recent news ─► price alert
                           (rules, baskets,   (one alert per
                            cooldowns)          sector move)
```

```
news247/
  sources/      rss, sec_edgar, halts, pagewatch, bluesky, social (x, mastodon), apis (hn, reddit, finnhub)
  analysis/     scorer, entities, dedup (story clustering), llm
  market/       detector (move rules, baskets), prices (yahoo, finnhub)
  notify/       channels + dispatcher (severity routing, quiet hours, rate limit)
  web/          dashboard + API
  data/         knowledge.yaml (companies, keywords, themes, baskets), default_sources.yaml, config.example.yaml
  engine.py     the pipeline · storage.py SQLite · cli.py
```

## Development

```bash
pip install -e ".[dev]"
pytest -q          # 130+ tests: parsers on real feed formats, scoring calibration, move detection,
                   # every notification channel's wire format, LLM client, engine end-to-end, web API
ruff check . && ruff format --check .
```

---

*For information only, not financial advice. Respect each source's terms of use and rate limits: the SEC asks for a descriptive User-Agent and at most 10 requests/second, which the HTTP client enforces for you.*
