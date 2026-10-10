# Alpaca: options data for the tape, and an MCP server for Claude

One Alpaca account (a free paper-trading account is enough) gives you two things:

1. **The options tape reads chains from Alpaca.** Cboe stays as the automatic fallback.
2. **Claude Code can query Alpaca directly** through Alpaca's official MCP server. It is set up
   read-only: it cannot place orders.

## 1. Keys on your server

Create API keys at [app.alpaca.markets](https://app.alpaca.markets): *Paper Trading → API
Keys*. Paper keys start with `PK`. They can't touch real money and they read market data the
same way live keys do.

Add them where the server reads its settings. Never put them in the repository; it is public.

| Where it runs | How |
|---|---|
| Render | the service → **Environment** → add `ALPACA_KEY` and `ALPACA_SECRET` → Save (Render redeploys) |
| Docker / systemd / Mac | add `ALPACA_KEY=…` and `ALPACA_SECRET=…` to the `.env` next to `config.yaml` |

That's all. With `options.provider: auto` (the default), the tape switches to Alpaca when both
keys are present and keeps Cboe as the fallback. Optional extra: `ALPACA_ENABLED=true` also
turns on Alpaca's real-time Benzinga news feed, which uses the same keys.

**Check it from the server:**

```bash
news247 options --provider alpaca NVDA NWE
```

Each symbol prints `source: Alpaca indicative feed …`, the contracts read, today's biggest
movers and any alert it would send. `GET /api/options` and `/api/status` show each source's
reads, errors and last success, and how many chains came from the fallback.

## What Alpaca gives the tape

Per company, one chain-snapshot request (1,000 contracts a page):

- **Quote and last trade:** bid, ask, last trade price and time.
- **Daily bars:** today's bar (volume) and the previous one (the previous close).
- **Implied volatility** for each contract.

Once a day per company, one request to the option-contracts list gives **open interest**.

If open interest can't be read, unusual volume is not judged for that chain, so it can never
raise a fake "no open interest" alert. Spikes are still judged.

The stock's price comes from the Nasdaq universe (refreshed every 10 minutes in session). The
company's 30-day IV, used by the stale-base check, is the median IV of near-the-money contracts
7 to 60 days out.

| Plan | Feed (`alpaca_feed`) | What it means |
|---|---|---|
| Free (Basic) | `indicative` (default) | Trades about 15 minutes delayed; quotes are Alpaca's adjusted "indicative" quotes, not the exchange's exact bid/ask. 200 requests a minute: a full pass over ~3,000 companies takes roughly 20 to 30 minutes, and hot names are still re-read every 3 minutes. |
| Algo Trader Plus (paid) | `opra` | Real-time OPRA trades and quotes, 10,000 requests a minute. Set `ALPACA_OPTIONS_FEED=opra` and lower `options.alpaca_request_interval_s` (e.g. 0.01). |

On the free feed, the "bid-confirmed" test uses indicative quotes. They follow the real market
but are not the exact NBBO, so treat the bid in an alert as approximate. Every alert names its
feed.

**Fallback:** if Alpaca fails a read (an error, a timeout or a bad answer), the same chain is
read from Cboe and counted under `fallbacks`. If a source says ten $10B+ companies in a row
"have no options", the tape treats that as a failure (a blocked server looks exactly like
that) rather than believing it. A feed that keeps failing sends one system alert a day.

To use only one source: `OPTIONS_PROVIDER=alpaca` or `cboe`, or `options: {fallback: false}`.

**Live keys:** open interest is read from the paper endpoint by default. With live (non-paper)
keys, set `ALPACA_TRADING_URL=https://api.alpaca.markets`. Otherwise you get one alert a day:
"unusual-volume alerts are paused".

## 2. Alpaca as an MCP server for Claude Code

The repo ships `.mcp.json`. Claude Code offers to load it when you open the project:

```json
{"mcpServers": {"alpaca": {"command": "uvx", "args": ["alpaca-mcp-server==2.3.2"],
  "env": {"ALPACA_API_KEY": "${ALPACA_KEY:-}", "ALPACA_SECRET_KEY": "${ALPACA_SECRET:-}",
          "ALPACA_PAPER_TRADE": "true",
          "ALPACA_TOOLSETS": "assets,stock-data,options-data,news,corporate-actions"}}}}
```

**Read-only by construction.** `ALPACA_TOOLSETS` leaves out the `trading`, `account`,
`watchlists` and `locates` toolsets. Checked on version 2.3.2:

- with this setting it exposes **34 tools**, none of which can place, cancel or replace orders,
  close positions, exercise options or change the account;
- the full server exposes 72, including `place_option_order`.

The options tools it keeps are `get_option_chain`, `get_option_snapshot`, `get_option_contracts`
(open interest), `get_option_bars`, `get_option_trades`, `get_option_latest_quote` and
`get_option_latest_trade`. It also keeps stock data, news and corporate actions.
`ALPACA_PAPER_TRADE=true` additionally points everything at the paper account.

**On your own computer:** install [uv](https://docs.astral.sh/uv/), export `ALPACA_KEY` and
`ALPACA_SECRET` in your shell, then open the project in Claude Code and approve the `alpaca`
server.

**In Claude Code on the web (cloud sessions):** open the environment's settings (the cloud
environment menu in the session's title bar → Edit):

1. **Environment variables:** add `ALPACA_KEY` and `ALPACA_SECRET`.
2. **Network access:** add `data.alpaca.markets` and `paper-api.alpaca.markets` under *Allowed
   domains*. `pypi.org` is already allowed, so `uvx` can install the server. Add
   `cdn.cboe.com` too, so sessions can read the Cboe fallback.
3. Start a new session: the `alpaca` tools load from `.mcp.json`.

**Not recommended: a claude.ai custom connector.** That would mean hosting the server on a
public URL. The server has no login of its own, so anyone who found the URL could use your keys.
