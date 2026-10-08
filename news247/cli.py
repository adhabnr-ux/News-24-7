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

    scorer = Scorer(cfg.scoring, cfg.knowledge, cfg.market.symbols)
    tier = SourceTier[args.tier.upper()]
    item = NewsItem(source=args.source, title=" ".join(args.headline), summary=args.summary or "", tier=tier)
    if args.entity:
        item.extra["entities"] = args.entity
    a = scorer.explain(item)
    print(f"\n  {a.severity.emoji} {a.severity.name}  score {a.score:.1f}/100   direction: {a.direction}")
    print(f"  tickers:  {' '.join(a.tickers) or '-'}")
    print(f"  entities: {', '.join(a.entities) or '-'}")
    print(f"  themes:   {', '.join(a.themes) or '-'}\n")
    for r in a.reasons:
        print(f"    {r}")
    t = cfg.scoring
    print(f"\n  thresholds: medium {t.medium:.0f} · high {t.high:.0f} · critical {t.critical:.0f}\n")
    return 0


# --------------------------------------------------------------------------- test-notify


async def _test_notify(cfg: Config) -> int:
    from .analysis.scorer import Scorer
    from .http import HttpClient
    from .notify import Dispatcher

    async with HttpClient(cfg.general.user_agent) as http:
        disp = Dispatcher(cfg.notify, http)
        if not disp.channels:
            print("No notification channels enabled. Configure one under `notify:` in config.yaml.")
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
    return asyncio.run(_test_notify(cfg))


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
            "# Secrets referenced from config.yaml as ${NAME}\nNTFY_TOPIC=\nTELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n"
            "DISCORD_WEBHOOK_URL=\nFINNHUB_TOKEN=\nX_BEARER_TOKEN=\nREDDIT_CLIENT_ID=\nREDDIT_CLIENT_SECRET=\n"
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

    sub.add_parser("test-notify", help="send a test alert to every enabled channel")

    d = sub.add_parser("demo", help="simulated AI-launch → software selloff scenario (no network needed)")
    d.add_argument("--speed", type=float, default=1.0, help="playback speed multiplier")

    st = sub.add_parser("stats", help="latency per source")
    st.add_argument("--days", type=float, default=7)

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
}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
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
        setup_logging(cfg, args.verbose)
    else:
        logging.basicConfig(
            level=logging.DEBUG if args.verbose else logging.ERROR,
            format="%(levelname)s %(name)s: %(message)s",
        )
    return COMMANDS[args.cmd](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
