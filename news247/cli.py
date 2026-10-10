"""Command-line interface.

news247 run            start monitoring (dashboard on http://127.0.0.1:8247)
news247 check          fetch every source once and report what works from this machine
news247 score "..."    explain how a headline would be scored
news247 test-notify    send a test alert to every enabled channel
news247 demo           replay a simulated "AI launch → software stocks crash" scenario
news247 stats          detection latency per source from the database
news247 init           write a starter config.yaml and .env
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import logging.handlers
import signal
import sys
import time
from importlib import resources
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .models import Alert, NewsItem, PriceMove, Severity, SourceTier

log = logging.getLogger("news247")


def ensure_data_dir(cfg: Config) -> None:
    """Make sure the data folder is writable; otherwise fall back to a temp folder (with a
    loud warning) so the monitor still runs instead of crash-looping."""
    path = cfg.data_path
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        import tempfile

        fallback = Path(tempfile.gettempdir()) / "news247-data"
        fallback.mkdir(parents=True, exist_ok=True)
        cfg.general.data_dir = str(fallback)
        cfg.path = None
        print(
            f"WARNING: data folder {path} is not writable ({exc}); using {fallback} instead. "
            "History and settings will not survive a restart until the folder is writable.",
            file=sys.stderr,
        )


def setup_logging(cfg: Config, verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else getattr(logging, cfg.general.log_level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(fmt)
    root.addHandler(console)
    try:
        cfg.data_path.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            cfg.data_path / "news247.log", maxBytes=5_000_000, backupCount=3
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError as exc:
        log.warning("file logging disabled: %s", exc)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError, RuntimeError):  # Windows
            loop.add_signal_handler(sig, stop.set)


# --------------------------------------------------------------------------- run


async def _run(cfg: Config) -> None:
    from .engine import Engine

    stop = asyncio.Event()
    _install_signal_handlers(stop)
    engine = Engine(cfg)
    try:
        await engine.run(stop)
    except KeyboardInterrupt:
        stop.set()


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    if args.no_web:
        cfg.web.enabled = False
    if args.no_market:
        cfg.market.enabled = False
    if args.port:
        cfg.web.port = args.port
    if args.host:
        cfg.web.host = args.host
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run(cfg))
    return 0


# --------------------------------------------------------------------------- check


async def _check(cfg: Config, only: list[str]) -> int:
    from .http import HttpClient
    from .sources import SourceContext, build_sources

    async with HttpClient(cfg.general.user_agent, cfg.general.http_timeout_s) as http:
        ctx = SourceContext(
            http, cfg.general.user_agent, cfg.general.max_item_age_minutes * 60, cfg.data_path
        )
        sources = build_sources(cfg.sources, ctx, include_disabled=bool(only))
        if only:
            sources = [s for s in sources if s.name in only]
        print(f"Checking {len(sources)} sources from this machine…\n")
        sem = asyncio.Semaphore(8)
        results: list[tuple[str, str, str, int, float, str]] = []

        async def one(src: object) -> None:
            async with sem:
                t0 = time.monotonic()
                try:
                    items = await asyncio.wait_for(src.check(), timeout=40)  # type: ignore[attr-defined]
                    newest = max(
                        (i for i in items if i.published), key=lambda i: i.published or 0, default=None
                    )
                    sample = newest or (items[0] if items else None)
                    note = sample.title[:70] if sample else "(no items)"
                    age = (
                        f"{(time.time() - newest.published) / 3600:.1f}h"
                        if newest and newest.published
                        else "-"
                    )
                    results.append((src.name, "OK", age, len(items), time.monotonic() - t0, note))  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001
                    results.append(
                        (src.name, "FAIL", "-", 0, time.monotonic() - t0, f"{type(exc).__name__}: {exc}"[:90])
                    )  # type: ignore[attr-defined]

        await asyncio.gather(*(one(s) for s in sources))
        if cfg.market.enabled and not only:
            from .market.detector import MoveDetector
            from .market.prices import PriceMonitor

            pm = PriceMonitor(cfg.market, http, MoveDetector(cfg.market))
            t0 = time.monotonic()
            try:
                if cfg.market.provider == "yahoo":
                    await pm.poll_yahoo_once()
                    n = len(pm.detector.last_price)
                    results.append(
                        (
                            f"prices:{cfg.market.provider}/{pm.mode}",
                            "OK",
                            "-",
                            n,
                            time.monotonic() - t0,
                            f"{n}/{len(cfg.market.symbols)} symbols priced",
                        )
                    )
                else:
                    results.append(
                        ("prices:finnhub", "SKIP", "-", 0, 0.0, "websocket feed — verified by `news247 run`")
                    )
            except Exception as exc:  # noqa: BLE001
                results.append(
                    (f"prices:{cfg.market.provider}", "FAIL", "-", 0, time.monotonic() - t0, str(exc)[:90])
                )

    width = max(len(r[0]) for r in results) if results else 10
    print(f"{'SOURCE':<{width}}  STATUS  NEWEST  ITEMS   TIME  SAMPLE / ERROR")
    for name, status, age, n, dt, note in sorted(results, key=lambda r: (r[1] != "FAIL", r[0])):
        mark = "\033[32m" if status == "OK" else ("\033[31m" if status == "FAIL" else "\033[33m")
        reset = "\033[0m"
        if not sys.stdout.isatty():
            mark = reset = ""
        print(f"{name:<{width}}  {mark}{status:<6}{reset}  {age:>6}  {n:>5}  {dt:4.1f}s  {note}")
    failed = sum(1 for r in results if r[1] == "FAIL")
    print(
        f"\n{len(results) - failed}/{len(results)} OK.",
        "Failing sources are retried with backoff at runtime;"
        " disable them with `enabled: false` in config.yaml if they never work from your network."
        if failed
        else "",
    )
    return 1 if failed == len(results) and results else 0


def cmd_check(cfg: Config, args: argparse.Namespace) -> int:
    return asyncio.run(_check(cfg, args.sources))


# --------------------------------------------------------------------------- score


def cmd_score(cfg: Config, args: argparse.Namespace) -> int:
    from .analysis.scorer import Scorer
    from .market.radar import SmallCapFeed

    scorer = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    uni = SmallCapFeed.load_cached(cfg.data_path) if cfg.smallcap.enabled else None
    if uni is not None:  # small caps are sized against the market caps `news247 universe` saved
        scorer.attach_universe(uni, **cfg.smallcap.filters())
    tier = SourceTier[args.tier.upper()]
    item = NewsItem(
        source=args.source,
        title=" ".join(args.headline),
        summary=args.summary or "",
        tier=tier,
        tickers=[t.upper() for t in (args.ticker or [])],
    )
    if args.entity:
        item.extra["entities"] = args.entity
    a = scorer.explain(item)
    print(f"\n  {a.severity.emoji} {a.severity.name}  score {a.score:.1f}/100   direction: {a.direction}")
    print(f"  tickers:  {' '.join(a.tickers) or '-'}")
    print(f"  entities: {', '.join(a.entities) or '-'}")
    print(f"  themes:   {', '.join(a.themes) or '-'}")
    if a.smallcap:
        sc = a.smallcap
        print(
            f"  small cap: {sc['symbol']} {sc['cap']} {sc['band']} · {sc.get('label', '-')} · {sc.get('move_text', '')}"
        )
    elif uni is None and cfg.smallcap.enabled:
        print("  small cap: no universe cached yet (run `news247 universe` once to size small caps)")
    print()
    for r in a.reasons:
        print(f"    {r}")
    t = cfg.scoring
    print(f"\n  thresholds: medium {t.medium:.0f} · high {t.high:.0f} · critical {t.critical:.0f}\n")
    return 0


# --------------------------------------------------------------------------- universe


async def _universe(cfg: Config, symbols: list[str], refresh: bool) -> int:
    from .http import HttpClient
    from .market.radar import SmallCapFeed
    from .market.universe import Universe, fmt_cap

    uni = SmallCapFeed.load_cached(cfg.data_path) or Universe()
    if refresh or not len(uni):
        cfg.data_path.mkdir(parents=True, exist_ok=True)
        async with HttpClient(cfg.general.user_agent, timeout_s=60) as http:
            feed = SmallCapFeed(cfg.smallcap, http, uni, cfg.data_path)
            feed._saved_at = 0.0
            if await feed.refresh_universe() is None:
                print(f"  could not load the Nasdaq screener: {feed.health.last_error}")
                if not len(uni):
                    return 1
                print(f"  using the cached universe ({len(uni)} listings)")
            else:
                uni.save(cfg.data_path / "universe.json.gz")
    bands: dict[str, int] = {}
    for li in uni.by_symbol.values():
        if li.common:
            bands[li.band] = bands.get(li.band, 0) + 1
    age = "?" if uni.age == float("inf") else f"{uni.age / 3600:.1f}h old"
    print(f"\n  {len(uni)} listings ({uni.source}, {age})")
    for b in ("mega cap", "large cap", "mid cap", "small cap", "micro cap", "nano cap", "unknown size"):
        if bands.get(b):
            print(f"    {b:<13} {bands[b]:>5}")
    lo, hi = cfg.smallcap.min_market_cap, cfg.smallcap.max_market_cap
    lane = sum(
        1 for li in uni.by_symbol.values() if li.common and li.market_cap and lo <= li.market_cap <= hi
    )
    print(f"  small-cap lane ({fmt_cap(lo)}–{fmt_cap(hi)}): {lane} companies sized against their news\n")
    for sym in symbols:
        li = uni.get(sym) or uni.lookup_name(sym)
        if li is None:
            print(f"  {sym}: not listed")
            continue
        print(f"  {li.symbol}  {li.name}")
        print(
            f"     {li.cap_label} {li.band} · ${li.price or 0:,.2f} · {li.sector or '-'} / {li.industry or '-'}"
        )
        if li.pump_profile:
            print(f"     ⚠ {li.pump_profile}")
    return 0


def cmd_universe(cfg: Config, args: argparse.Namespace) -> int:
    return asyncio.run(_universe(cfg, args.symbols, args.refresh))


# --------------------------------------------------------------------------- options


async def _options(cfg: Config, symbols: list[str], min_pct: float | None) -> int:
    """Read option chains once from this machine and show what the options tape sees."""
    import dataclasses
    from datetime import datetime

    from .http import HttpClient
    from .market.options import ET, OptionsFeed, OptionsRadar
    from .market.radar import SmallCapFeed
    from .market.universe import Universe, fmt_cap

    ocfg = cfg.options if min_pct is None else dataclasses.replace(cfg.options, spike_min_pct=min_pct)
    uni = SmallCapFeed.load_cached(cfg.data_path) or Universe()
    covered = sum(
        1 for li in uni.by_symbol.values() if li.common and (li.market_cap or 0) >= ocfg.min_market_cap
    )
    print(f"\n  options tape: {covered} companies of {fmt_cap(ocfg.min_market_cap)}+ in the cached universe")
    if not len(uni):
        print("  (no cached universe yet: run `news247 universe --refresh`)")
    worst = 0
    async with HttpClient(cfg.general.user_agent, timeout_s=60) as http:
        feed = OptionsFeed(ocfg, http, uni, OptionsRadar(ocfg))
        for sym in [s.upper() for s in symbols]:
            print(f"\n  {sym}  {feed.url(sym)}")
            try:
                chain = await feed.fetch_chain(sym)
            except Exception as exc:  # noqa: BLE001 - show any failure to the operator
                print(f"     failed: {type(exc).__name__}: {exc}")
                worst = 1
                continue
            if chain is None:
                print("     no option chain listed for this symbol")
                continue
            today = datetime.now(ET).date()
            live = [c for c in chain.contracts if c.expiry >= today]
            expiries = sorted({c.expiry for c in live})
            when = f"{chain.quote_time:%Y-%m-%d %H:%M} ET" if chain.quote_time else "no timestamp"
            stock = f"${chain.price:,.2f}" if chain.price else "?"
            chg = f" ({chain.change_pct:+.2f}%)" if chain.change_pct is not None else ""
            print(
                f"     stock {stock}{chg} · {len(live):,} live contracts over {len(expiries)} expiries "
                f"· quotes as of {when}" + (f" · {chain.skipped} adjusted skipped" if chain.skipped else "")
            )
            traded = [c for c in live if c.volume > 0 and c.move_pct is not None]
            print(f"     {sum(c.volume for c in live):,} contracts traded today in {len(traded):,} contracts")
            for c in sorted(traded, key=lambda c: c.move_pct or 0, reverse=True)[:5]:
                bid = f"{c.bid:.2f}" if c.bid is not None else "-"
                ask = f"{c.ask:.2f}" if c.ask is not None else "-"
                print(
                    f"       {c.label():<34} {c.move_pct:+9,.0f}%  ${c.prev_close or 0:.2f} -> ${c.last or 0:.2f}"
                    f"  bid/ask {bid}/{ask}  vol {c.volume:,}  OI {c.open_interest:,}"
                )
            hits = feed.radar.scan(chain, uni.get(sym))
            if not hits:
                print(f"     no alert: nothing at {ocfg.spike_min_pct:,.0f}%+ or unusual volume right now")
            for h in hits:
                d = h.to_dict()
                if h.kind == "spike":
                    top = h.spikes[0]
                    print(
                        f"     ALERT {h.severity}: spike {top.contract.label()} {top.honest_move_pct:+,.0f}% "
                        f"(confirmed={top.confirmed}, stale_base={top.stale_base})"
                    )
                else:
                    print(
                        f"     ALERT {h.severity}: unusual volume {fmt_cap(d['flow_premium'])} in {len(h.flow)} contracts"
                    )
                for r in h.reasons:
                    print(f"       ⚠ {r}")
    print()
    return worst


def cmd_options(cfg: Config, args: argparse.Namespace) -> int:
    return asyncio.run(_options(cfg, args.symbols, args.min_pct))


# --------------------------------------------------------------------------- test-notify


async def _test_notify(cfg: Config, only: list[str] | None = None) -> int:
    from .analysis.scorer import Scorer
    from .http import HttpClient
    from .notify import Dispatcher

    async with HttpClient(cfg.general.user_agent) as http:
        disp = Dispatcher(cfg.notify, http)
        if only:
            disp.channels = [c for c in disp.channels if c.name in only]
        if not disp.channels:
            which = f" matching {', '.join(only)}" if only else ""
            print(f"No notification channels enabled{which}. Configure one under `notify:` in config.yaml.")
            return 1
        item = NewsItem(
            source="news247-test",
            title="TEST: OpenAI launches autonomous agents for enterprise accounting and legal work",
            url="https://github.com/adhabnr-ux/News-24-7",
            tier=SourceTier.PRIMARY,
            published=time.time() - 3,
            extra={"entities": ["OpenAI"]},
        )
        analysis = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols).score(item)
        analysis.summary = "This is a test alert from News247 — if you can read this, the channel works."
        alert = Alert(
            kind="news",
            severity=Severity.CRITICAL,
            title=item.title,
            body="(test message)",
            url=item.url,
            tickers=analysis.tickers,
            item=item,
            analysis=analysis,
        )
        # bypass per-channel severity floors: test every enabled channel
        results = await asyncio.gather(*(disp._send(ch, alert) for ch in disp.channels))
        for ch, res in zip(disp.channels, results):
            print(f"  {ch.name:<10} {res}")
        return 0 if all(r == "ok" for r in results) else 1


def cmd_test_notify(cfg: Config, args: argparse.Namespace) -> int:
    return asyncio.run(_test_notify(cfg, args.only))


# --------------------------------------------------------------------------- demo


async def _demo(cfg: Config, speed: float) -> None:
    """Run the full pipeline (dashboard + notifications) on a scripted scenario, no network needed."""
    from .engine import Engine
    from .storage import Storage
    from .web.server import WebServer

    cfg.market.enabled = False
    engine = Engine(cfg, storage=Storage(":memory:"), sources=[])
    stop = asyncio.Event()
    _install_signal_handlers(stop)
    await engine.http.start()
    web = WebServer(engine, cfg.web) if cfg.web.enabled else None
    if web:
        await web.start()
    det = engine.detector
    base = time.time() - 900
    software = [
        "CRM",
        "NOW",
        "ADBE",
        "INTU",
        "WDAY",
        "TEAM",
        "HUBS",
        "DDOG",
        "SNOW",
        "MDB",
        "PANW",
        "CRWD",
        "DOCU",
        "MNDY",
        "IGV",
    ]
    start_px = {s: 100.0 + i * 13 for i, s in enumerate(software + ["SPY", "QQQ"])}
    for s, p in start_px.items():  # 15 calm minutes of history
        det.seed(s, [(base + k * 30, p * (1 + 0.0004 * ((k % 5) - 2))) for k in range(30)])

    def news(
        title: str, source: str, tier: SourceTier, ents: list[str] | None = None, url: str = ""
    ) -> NewsItem:
        return NewsItem(
            source=source,
            title=title,
            tier=tier,
            published=time.time() - 2,
            url=url or f"https://example.com/demo/{abs(hash(title))}",
            extra={"entities": ents or []},
        )

    script = [
        (
            1,
            "item",
            news(
                "Nvidia to Present at Upcoming Investor Conferences",
                "nvidia-newsroom",
                SourceTier.PRIMARY,
                ["NVIDIA"],
            ),
        ),
        (
            2,
            "item",
            news(
                "SHAREHOLDER ALERT: Rosen Law Firm reminds investors of class action against XYZ",
                "globenewswire",
                SourceTier.WIRE,
            ),
        ),
        (
            3,
            "item",
            news(
                "Introducing ChatGPT Agents for finance, legal and sales teams",
                "openai-news",
                SourceTier.PRIMARY,
                ["OpenAI"],
                "https://openai.com/news/",
            ),
        ),
        (
            4,
            "item",
            news(
                "@DeItaone: *OPENAI LAUNCHES ENTERPRISE AGENTS FOR FINANCE, LEGAL AND SALES",
                "x",
                SourceTier.PRIMARY,
            ),
        ),
        (5, "prices", -0.012),
        (
            7,
            "item",
            news(
                "OpenAI launches ChatGPT agents for finance, legal and sales teams",
                "cnbc-tech",
                SourceTier.MEDIA,
            ),
        ),
        (9, "prices", -0.028),
        (
            11,
            "item",
            news(
                "Software stocks tumble after OpenAI unveils AI agents for enterprise work",
                "mw-bulletins",
                SourceTier.MEDIA,
            ),
        ),
        (13, "prices", -0.05),
    ]
    print(
        "\nDemo running — open the dashboard:", f"http://localhost:{cfg.web.port}/" if web else "(disabled)"
    )
    t_last = 0.0
    for t, kind, payload in script:
        await asyncio.sleep((t - t_last) / speed)
        t_last = t
        if stop.is_set():
            break
        if kind == "item":
            await engine.on_item(payload)  # type: ignore[arg-type]
        else:
            now = time.time()
            moves: list[PriceMove] = []
            for i, s in enumerate(software):
                drop = payload * (0.7 + 0.05 * (i % 7))  # type: ignore[operator]
                moves.extend(det.update(s, start_px[s] * (1 + drop), now))
            det.update("SPY", start_px["SPY"] * (1 + payload * 0.12), now)  # type: ignore[operator]
            moves.extend(det.check_groups(now))
            if moves:
                await engine.on_moves(moves)
    await engine.drain(10)
    if web:
        print("\nScenario complete. Dashboard stays up — Ctrl+C to exit.")
        await stop.wait()
        await web.stop()
    await engine.http.close()


def cmd_demo(cfg: Config, args: argparse.Namespace) -> int:
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_demo(cfg, args.speed))
    return 0


# --------------------------------------------------------------------------- stats / init


def cmd_stats(cfg: Config, args: argparse.Namespace) -> int:
    from .storage import Storage

    db = cfg.data_path / "news247.db"
    if not db.exists():
        print(f"No database yet at {db}. Run `news247 run` first.")
        return 1
    st = Storage(db)
    print(st.counts())
    rows = st.latency_stats(time.time() - args.days * 86400)
    if not rows:
        print("No items with publish times yet.")
        return 0
    print(f"\nDetection latency (publish → detected), last {args.days:g} days:\n")
    print(f"{'SOURCE':<26} {'ITEMS':>6} {'MEDIAN':>8} {'P90':>8}")
    for r in sorted(rows, key=lambda r: r["median_s"]):
        print(f"{r['source']:<26} {r['items']:>6} {r['median_s']:>7.0f}s {r['p90_s']:>7.0f}s")
    print(
        "\nNote: lag includes the publisher's own feed delay (many blogs update RSS minutes after posting)."
    )
    wins = st.first_seen_stats(time.time() - args.days * 86400)
    if wins:
        print("\nWho had the story first (stories seen by 2+ sources):\n")
        print(f"{'SOURCE':<26} {'FIRST':>6} {'MEDIAN LEAD':>12}")
        for w in wins[:20]:
            print(f"{w['source']:<26} {w['first']:>6} {w['median_lead_s']:>11.0f}s")
    return 0


def cmd_backtest(cfg: Config, args: argparse.Namespace) -> int:
    from .backtest import format_report, run_backtest

    rep = run_backtest(cfg, threshold=Severity.parse(args.threshold))
    print(format_report(rep, verbose=args.verbose))
    return 0


def cmd_init(cfg: Config | None, args: argparse.Namespace) -> int:
    dest = Path(args.path)
    if dest.exists() and not args.force:
        print(f"{dest} already exists (use --force to overwrite).")
        return 1
    dest.write_text(
        resources.files("news247.data").joinpath("config.example.yaml").read_text(encoding="utf-8")
    )
    env = dest.parent / ".env"
    if not env.exists():
        env.write_text(
            "# Secrets referenced from config.yaml as ${NAME}\n"
            "CONTACT_EMAIL=\n"
            "# iMessage from this Mac (deploy/install-macos.sh fills these in)\nIMESSAGE_ENABLED=false\nIMESSAGE_TO=\n"
            "NTFY_TOPIC=\nTELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\nDISCORD_WEBHOOK_URL=\n"
            "FINNHUB_TOKEN=\nX_BEARER_TOKEN=\nREDDIT_CLIENT_ID=\nREDDIT_CLIENT_SECRET=\n"
        )
    print(f"Wrote {dest} and {env}. Edit them, then run `news247 check` and `news247 run`.")
    return 0


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="news247", description="24/7 market-moving news monitor")
    p.add_argument("-c", "--config", help="path to config.yaml (default: ./config.yaml if present)")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"news247 {__version__}")
    sub = p.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="start monitoring")
    r.add_argument("--no-web", action="store_true", help="don't start the dashboard")
    r.add_argument("--no-market", action="store_true", help="don't watch prices")
    r.add_argument("--port", type=int, help="dashboard port")
    r.add_argument("--host", help="dashboard bind address (0.0.0.0 = all interfaces)")

    c = sub.add_parser("check", help="test every source once")
    c.add_argument("sources", nargs="*", help="only these source names")

    s = sub.add_parser("score", help="explain the score of a headline")
    s.add_argument("headline", nargs="+")
    s.add_argument("--tier", default="media", choices=[t.name.lower() for t in SourceTier])
    s.add_argument("--source", default="cli")
    s.add_argument("--summary", default="")
    s.add_argument(
        "--entity", action="append", help="entity hint, e.g. --entity OpenAI (as if posted on its blog)"
    )
    s.add_argument("--ticker", action="append", help="ticker the source attached, e.g. --ticker ACMB")

    u = sub.add_parser("universe", help="load every listed company's market cap; look up symbols")
    u.add_argument("symbols", nargs="*", help="symbols or company names to show")
    u.add_argument("--refresh", action="store_true", help="download a fresh copy now")

    o = sub.add_parser("options", help="read option chains once: biggest movers, unusual volume, alerts")
    o.add_argument("symbols", nargs="+", help="symbols, e.g. NVDA NWE")
    o.add_argument("--min-pct", type=float, help="spike threshold for this run (default from config: 1000)")

    tn = sub.add_parser("test-notify", help="send a test alert to every enabled channel")
    tn.add_argument(
        "--only", action="append", help="test just this channel (repeatable), e.g. --only imessage"
    )

    d = sub.add_parser("demo", help="simulated AI-launch → software selloff scenario (no network needed)")
    d.add_argument("--speed", type=float, default=1.0, help="playback speed multiplier")

    bt = sub.add_parser("backtest", help="score historical market-moving headlines and noise")
    bt.add_argument("--threshold", default="high", choices=["medium", "high", "critical"])
    bt.add_argument("-v", "--verbose", action="store_true", help="show scoring reasons")

    st = sub.add_parser("stats", help="latency per source")
    st.add_argument("--days", type=float, default=7)

    sub.add_parser(
        "relay",
        help="run the iMessage relay on a Mac: news247 relay [run|doctor|send-test] (see news247 relay -h)",
        add_help=False,
    )

    i = sub.add_parser("init", help="write a starter config.yaml")
    i.add_argument("--path", default="config.yaml")
    i.add_argument("--force", action="store_true")
    return p


COMMANDS = {
    "run": cmd_run,
    "check": cmd_check,
    "score": cmd_score,
    "test-notify": cmd_test_notify,
    "demo": cmd_demo,
    "stats": cmd_stats,
    "backtest": cmd_backtest,
    "universe": cmd_universe,
    "options": cmd_options,
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["relay"]:  # the Mac-side relay has its own options and needs no config.yaml
        from .relay.agent import main as relay_main

        return relay_main(argv[1:])
    args = parser.parse_args(argv)
    if args.cmd is None:  # bare `news247` means `news247 run`
        args = parser.parse_args([*argv, "run"])
    if args.cmd == "init":
        return cmd_init(None, args)
    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    if args.cmd in ("run", "demo"):
        ensure_data_dir(cfg)
        setup_logging(cfg, args.verbose)
    else:
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.ERROR,
            format="%(levelname)s %(name)s: %(message)s",
        )
    return COMMANDS[args.cmd](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
