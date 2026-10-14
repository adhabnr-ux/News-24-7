"""The whole-market tape (Alpaca snapshots or Yahoo quotes) and what the engine does with an
ignition: confirmed by a headline, an insider buy or options on the same side, it is pushed;
unconfirmed, it goes on the board and its option chain is read at once."""

from __future__ import annotations

import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from news247.config import build_config
from news247.market.ignition import IgnitionHit, IgnitionScanner, Tick, session_minute, volume_share
from news247.market.options import AlpacaSource, Chain, OptionsHit
from news247.market.tape import TapeFeed, VolumeBase, parse_tape_snapshots, parse_yahoo_quotes
from news247.market.universe import Universe, with_otc
from news247.models import Alert, NewsItem, Severity, SourceTier

ET = ZoneInfo("America/New_York")
TODAY = date(2026, 10, 14)
UNI = {
    "ACME": {"name": "Acme Corp Common Stock", "mcap": 5e9, "price": 100.0},
    "BIGG": {"name": "Bigg Inc Common Stock", "mcap": 300e9, "price": 400.0},
    "TINY": {"name": "Tiny Co Common Stock", "mcap": 40e6, "price": 3.0},
}


def snap(price, ts, *, today=True, vol=50_000, prev_vol=40_000, hi=None, lo=None, vw=None):
    bar_day = "2026-10-14T04:00:00Z" if today else "2026-10-13T04:00:00Z"
    return {
        "latestTrade": {"p": price, "t": ts},
        "dailyBar": {
            "t": bar_day,
            "o": 99.0,
            "h": hi or price,
            "l": lo or 98.0,
            "c": price,
            "v": vol,
            "vw": vw or 99.5,
        },
        "prevDailyBar": {"t": "2026-10-13T04:00:00Z", "c": 98.0, "v": prev_vol},
    }


# --------------------------------------------------------------------------- parsing


def test_alpaca_snapshot_rows():
    doc = {
        "ACME": snap(101.5, "2026-10-14T14:31:02.123456789Z", hi=102.0, vw=100.4),
        "BRK.B": snap(500.0, "2026-10-14T14:31:00Z"),
        "EARLY": snap(20.0, "2026-10-14T08:01:00Z", today=False, vol=12_000),
        "BAD": {"latestTrade": {}},
    }
    rows = parse_tape_snapshots(doc, TODAY)
    a = rows["ACME"]
    assert (
        a["price"] == 101.5
        and a["prev_close"] == 98.0
        and a["volume"] == 50_000
        and a["prev_volume"] == 40_000
    )
    assert a["high"] == 102.0 and a["vwap"] == 100.4
    assert a["ts"] == pytest.approx(
        datetime(2026, 10, 14, 14, 31, 2, 123456, tzinfo=ZoneInfo("UTC")).timestamp()
    )
    assert "BRK-B" in rows and "BAD" not in rows
    # before today's first trade the daily bar is yesterday's: no volume today, that bar is "yesterday"
    e = rows["EARLY"]
    assert e["volume"] == 0 and e["prev_close"] == 20.0 and e["prev_volume"] == 12_000 and e["high"] is None
    assert parse_tape_snapshots({"snapshots": {"ACME": doc["ACME"]}}, TODAY)["ACME"]["price"] == 101.5


def test_yahoo_quote_rows():
    doc = {"quoteResponse": {"result": [
        {"symbol": "FNMA", "regularMarketPrice": 3.47, "regularMarketTime": 1791834300, "regularMarketPreviousClose": 4.04,
         "regularMarketVolume": 17_547_468, "averageDailyVolume10Day": 5_852_000, "regularMarketDayHigh": 3.905,
         "regularMarketDayLow": 3.24, "marketCap": 4.02e9, "longName": "Fannie Mae"},
        {"symbol": "NOPE", "regularMarketPrice": None},
    ]}}  # fmt: skip
    rows = parse_yahoo_quotes(doc)
    f = rows["FNMA"]
    assert f["volume"] == 17_547_468 and f["avg_volume"] == 5_852_000 and f["vwap"] is None
    assert f["high"] == 3.905 and f["name"] == "Fannie Mae" and "NOPE" not in rows
    assert parse_yahoo_quotes({}) == {}


def test_volume_base_moves_a_quarter_a_day_and_persists(tmp_path):
    vb = VolumeBase(tmp_path / "v.json")
    assert vb.update("ACME", 40_000, "2026-10-14") == 40_000
    assert vb.update("ACME", 999_999, "2026-10-14") == 40_000  # once a day
    assert vb.update("ACME", 80_000, "2026-10-15") == pytest.approx(50_000)  # 40k + 25% of 40k
    assert vb.update("NONE", None, "2026-10-15") is None
    vb.save()
    assert VolumeBase(tmp_path / "v.json").rows["ACME"][0] == 50_000


# --------------------------------------------------------------------------- the feed


def cfg_with(server, **sc):
    return build_config(
        {
            "options": {"alpaca_key": "k", "alpaca_secret": "s", "alpaca_data_url": server.url("/"), "alpaca_request_interval_s": 0.005},
            "smallcap": {"tape_batch": 2, **sc},
        }
    )  # fmt: skip


async def test_alpaca_sweep_reads_everyone_shares_prices_and_scans(server, http, tmp_path):
    from news247.market.live import LiveQuotes

    now = datetime(2026, 10, 14, 10, 30, tzinfo=ET).timestamp()
    iso = datetime.fromtimestamp(now - 2, ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%SZ")

    def answer(request):
        from aiohttp import web

        syms = request.query["symbols"].split(",")
        return web.json_response({s: snap(101.0, iso) for s in syms if s != "FNMA"})

    server.on("/v2/stocks/snapshots", answer)
    cfg = cfg_with(server)
    uni = Universe(with_otc(Universe.stub(UNI).by_symbol.values()))
    live = LiveQuotes()
    yahoo_calls = []

    async def yahoo_json(url, params):
        yahoo_calls.append(params["symbols"])
        return {"quoteResponse": {"result": [{"symbol": "FNMA", "regularMarketPrice": 3.5, "regularMarketTime": now - 5,
                                               "regularMarketPreviousClose": 4.04, "regularMarketVolume": 9e6,
                                               "averageDailyVolume10Day": 6e6}]}}  # fmt: skip

    feed = TapeFeed(
        cfg.smallcap, http, uni, IgnitionScanner(cfg.smallcap), live=live,
        alpaca=AlpacaSource(cfg.options, http), yahoo_json=yahoo_json, data_dir=tmp_path,
    )  # fmt: skip
    assert feed.provider == "alpaca" and feed.interval == 30
    assert "TINY" not in feed.symbols()  # under tape_min_cap
    assert feed.symbols()[0] == "BIGG"
    assert await feed.sweep(now) == []  # one look is not a move
    snaps = [r for r in server.requests if r["path"] == "/v2/stocks/snapshots"]
    assert len(snaps) == 2 and all(
        r["headers"]["APCA-API-KEY-ID"] == "k" for r in snaps
    )  # 4 names, 2 a request
    assert yahoo_calls == ["FNMA"]  # over the counter: Alpaca has no quote, Yahoo does
    q = live.get("ACME", 60, now)
    assert q.price == 101.0 and q.source == "tape:alpaca"
    assert live.get("FNMA", 60, now).price == 3.5
    assert feed.base.rows["ACME"][0] == 40_000 and (tmp_path / "tape_volume.json").exists()
    st = feed.status()
    assert st["provider"] == "alpaca" and st["covered"] == 4 and st["ticks"] == 4


async def test_yahoo_provider_without_keys(http, tmp_path):
    cfg = build_config({"smallcap": {"tape_seconds": 20}})
    uni = Universe.stub(UNI)

    async def yahoo_json(url, params):
        return {"quoteResponse": {"result": []}}

    feed = TapeFeed(cfg.smallcap, http, uni, IgnitionScanner(cfg.smallcap), yahoo_json=yahoo_json)
    assert feed.provider == "yahoo" and feed.interval == 60  # at most once a minute from Yahoo
    assert await feed.sweep(datetime(2026, 10, 14, 10, 30, tzinfo=ET).timestamp()) == []


async def test_a_failed_read_is_counted_not_raised(server, http):
    server.on("/v2/stocks/snapshots", (500, "down"))
    cfg = cfg_with(server)
    feed = TapeFeed(cfg.smallcap, http, Universe.stub(UNI), IgnitionScanner(cfg.smallcap),
                    alpaca=AlpacaSource(cfg.options, http))  # fmt: skip
    assert await feed.sweep(datetime(2026, 10, 14, 10, 30, tzinfo=ET).timestamp()) == []
    assert feed.health.consecutive_errors == 1 and "500" in feed.health.last_error


# --------------------------------------------------------------------------- the engine


def engine(tmp_path):
    from news247.engine import Engine
    from news247.notify import Dispatcher
    from news247.storage import Storage

    from .conftest import CaptureNotifier

    cfg = build_config({"general": {"data_dir": str(tmp_path)}, "notify": {"console": {"enabled": False}},
                        "smallcap": {"why_lookup": False}, "trends": {"scan": False}})  # fmt: skip
    Universe.stub(UNI).save(tmp_path / "universe.json.gz")
    disp = Dispatcher(cfg.notify, None)  # type: ignore[arg-type]
    disp.channels = [CaptureNotifier()]
    eng = Engine(cfg, storage=Storage(tmp_path / "e.db"), dispatcher=disp, sources=[])
    eng.scorer.attach_universe(eng.universe, **cfg.smallcap.filters())
    got: list[Alert] = []
    orig = eng.publish

    async def capture(alert, push=True):
        got.append(alert)
        await orig(alert, push)

    eng.publish = capture  # type: ignore[method-assign]
    return eng, got


def at(h, m):
    return datetime(2026, 10, 14, h, m, tzinfo=ET).timestamp()


def push(eng, *, move=2.0, burst=6.0, sym="ACME", start=(10, 30)):
    t0 = at(*start)
    m0 = session_minute(t0)
    v0 = 1_000_000 * volume_share(0, m0)
    eng.ignition.observe(
        [Tick(sym, t0, 100.0, 100.0, v0, 1_000_000, 100.0, 100.0, 100.0, 5e9, "Acme")], now=t0
    )
    p1 = 100.0 * (1 + move / 100)
    v1 = v0 + burst * 1_000_000 * volume_share(m0, m0 + 5)
    return eng.ignition.observe(
        [Tick(sym, t0 + 300, p1, 100.0, v1, 1_000_000, max(100, p1), min(100, p1), 100.2, 5e9, "Acme")],
        now=t0 + 300,
    )


async def test_tape_is_built_and_the_options_feed_leans_on_it(tmp_path):
    eng, _ = engine(tmp_path)
    assert eng.tape is not None and eng.ignition is not None
    assert eng.tape.provider == "yahoo"  # no Alpaca keys in tests
    assert eng.status()["tape"]["provider"] == "yahoo"


async def test_unconfirmed_ignition_goes_on_the_board_and_reads_its_chain(tmp_path):
    eng, got = engine(tmp_path)
    hits = push(eng)  # mid cap +2% on 6x: not "strong" (the day's volume is about normal)
    assert len(hits) == 1 and not hits[0].strong
    await eng.on_ignition(hits)
    assert got == []  # nothing pushed or shown
    row = eng.radar.board["ACME"]
    assert row["ignition"]["kind"] == "ignition" and row["source"] == "tape"
    assert "ACME" in eng.ignition_pending
    assert "ACME" in eng.options_feed._urgent and "igniting" in eng.options_feed._urgent["ACME"]


async def test_insider_buy_primes_then_the_push_is_confirmed_and_pushed(tmp_path):
    eng, got = engine(tmp_path)
    item = NewsItem(source="sec-insider-buys", title="CEO Jane Smith buys $2.5M of Acme Corp (ACME) stock at $99.10 (Form 4, open market)",
                    url="https://sec.gov/x", tier=SourceTier.PRIMARY, tickers=["ACME"],
                    extra={"insider_buy": {"owner": "Jane Smith", "role": "CEO", "value": 2_500_000}})  # fmt: skip
    an = eng.scorer.score(item)
    await eng.publish(
        Alert(
            kind="news",
            severity=Severity.HIGH,
            title=item.title,
            body="",
            tickers=["ACME"],
            item=item,
            analysis=an,
        )
    )
    assert eng.ignition.primed("ACME", "up", time.time())[0].startswith(
        "insider: CEO Jane Smith bought $2.5M"
    )
    got.clear()
    hits = push(eng, move=1.0, burst=3.5)  # primed: 0.6 x 1.5% and 3x are enough
    hits[0].detected = time.time()
    await eng.on_ignition(hits)
    (alert,) = got
    assert alert.severity == Severity.HIGH and alert.kind == "price"
    assert alert.title.startswith("⚡ ▲ ACME igniting: +1.00% in 5m on 4× normal volume")
    assert "What backs it: insider: CEO Jane Smith bought $2.5M (Form 4)" in alert.body
    assert alert.edge["ignition"]["confirmed_by"][0].startswith("insider:")
    assert "the move has failed under $100.20" in alert.body  # the VWAP, nearer than the $100.00 launch


async def test_options_on_the_same_side_confirm_a_waiting_ignition(tmp_path):
    eng, got = engine(tmp_path)
    hits = push(eng)
    hits[0].detected = time.time()
    await eng.on_ignition(hits)
    assert got == []
    flow = OptionsHit("ACME", "flow", "MEDIUM", Chain("ACME", 102.0, 100.0, 2.0), eng.universe.get("ACME"),
                      call_premium=1_800_000, put_premium=100_000)  # fmt: skip
    alert = Alert(
        kind="options", severity=Severity.MEDIUM, title="ACME options flow", body="", tickers=["ACME"]
    )
    waiting = eng._options_prime(flow, alert)
    assert waiting is hits[0]
    await eng._publish_ignition(waiting, eng._ignition_confirmations(waiting))
    (ign,) = got
    assert ign.severity == Severity.HIGH and "options: calls flow, $1.8M premium" in ign.body
    assert "ACME" not in eng.ignition_pending
    # puts do not confirm a move up
    eng.ignition_pending["ACME"] = (hits[0], time.time())
    puts = OptionsHit("ACME", "flow", "MEDIUM", Chain("ACME", 102.0, 100.0, 2.0), None, put_premium=900_000)
    assert (
        eng._options_prime(puts, Alert(kind="options", severity=Severity.MEDIUM, title="t", body="")) is None
    )


async def test_options_before_the_stock_moves_put_it_on_watch(tmp_path):
    eng, _ = engine(tmp_path)
    flow = OptionsHit("ACME", "flow", "MEDIUM", Chain("ACME", 100.3, 100.0, 0.3), eng.universe.get("ACME"),
                      call_premium=2_100_000)  # fmt: skip
    alert = Alert(kind="options", severity=Severity.MEDIUM, title="ACME options flow", body="Calls bought.")
    eng._options_prime(flow, alert)
    assert "The stock hasn't moved yet (+0.30%): it is on watch" in alert.body
    assert eng.ignition.primed("ACME", "up", time.time()) == ["options: calls flow, $2.1M premium"]
    moved = OptionsHit("ACME", "flow", "MEDIUM", Chain("ACME", 108.0, 100.0, 8.0), eng.universe.get("ACME"),
                       call_premium=2_100_000)  # fmt: skip
    a2 = Alert(kind="options", severity=Severity.MEDIUM, title="t", body="")
    eng._options_prime(moved, a2)
    assert "hasn't moved" not in a2.body  # already moving: remembered, not "on watch"
    assert eng.options_seen["ACME"][1] == "up"


async def test_strong_unconfirmed_ignitions_are_shown_and_pushes_are_capped(tmp_path):
    eng, got = engine(tmp_path)
    hit = IgnitionHit("ACME", "ignition", "up", 102.0, 100.0, 2.0, 100.0, 300, 2.0, 9.0, 3.5, 100.0,
                      market_cap=5e9, name="Acme", strong=True)  # fmt: skip
    await eng.on_ignition([hit])
    (shown,) = got
    assert shown.severity == Severity.MEDIUM and "Nothing backs it yet" in shown.body
    eng.cfg.smallcap.ignition_daily_pushes = 1
    got.clear()
    for sym in ("AAA", "BBB"):
        h = IgnitionHit(
            sym, "ignition", "up", 102.0, 100.0, 2.0, 100.0, 300, 2.0, 5.0, 1.2, 100.0, primed=["news: x"]
        )
        await eng._publish_ignition(h, ["news: x"])
    assert [a.severity for a in got] == [Severity.HIGH, Severity.MEDIUM]


async def test_radar_ladder_alert_warns_about_chasing_no_news_moves(tmp_path):
    from news247.market.radar import Quote, RadarHit

    eng, _ = engine(tmp_path)
    q = Quote("ACME", "Acme", 112.0, 100.0, 12.0, 2e6, 1e6, 5e9)
    alert = eng._radar_alert(RadarHit(q, "day", 10, ref_price=100.0))
    assert "63% of 68 such alerts closed the day back against the move" in alert.body


def test_board_lists_fresh_ignitions_first():
    from news247.market.radar import MoversRadar

    r = MoversRadar(build_config({}).smallcap)
    now = 1_791_900_000.0
    r.board = {
        "BIG": {"symbol": "BIG", "change_pct": 25.0},
        "IGN": {"symbol": "IGN", "change_pct": 2.0, "ignition": {"detected": now - 60}},
        "OLD": {"symbol": "OLD", "change_pct": 3.0, "ignition": {"detected": now - 7200}},
        "NEW": {"symbol": "NEW", "change_pct": -1.5, "ignition": {"detected": now - 10}},
    }
    assert [x["symbol"] for x in r.snapshot(now=now)] == ["NEW", "IGN", "BIG", "OLD"]


def test_tape_command_without_a_universe(tmp_path, capsys):
    import asyncio

    from news247.cli import _tape

    cfg = build_config({"general": {"data_dir": str(tmp_path)}})
    assert asyncio.run(_tape(cfg, 1, 0, 5)) == 1
    assert "no cached universe yet" in capsys.readouterr().out


async def test_tape_command_reads_and_reports(tmp_path, capsys, monkeypatch):
    from news247.cli import _tape
    from news247.market import tape as tape_mod

    Universe.stub(UNI).save(tmp_path / "universe.json.gz")
    cfg = build_config({"general": {"data_dir": str(tmp_path)}})

    async def fake_read(self, symbols):
        return {s: {"price": 101.0, "ts": time.time(), "prev_close": 100.0, "volume": 5e5, "base_volume": 1e6,
                    "high": 101.0, "low": 99.0, "vwap": None} for s in symbols}  # fmt: skip

    monkeypatch.setattr(tape_mod.TapeFeed, "read_yahoo", fake_read)
    assert await _tape(cfg, 1, 0, 5) == 0
    out = capsys.readouterr().out
    assert "tape: yahoo · 4 stocks of $100M+" in out and "sweep 1: 4 quotes" in out  # + Fannie and Freddie


@pytest.mark.parametrize(
    ("raw", "micro"),
    [("2026-10-14T14:31:02.123456789Z", 123456), ("2026-10-14T14:31:02.5Z", 500000), ("2026-10-14T14:31:02Z", 0),
     ("2026-10-14T10:31:02.25-04:00", 250000)],
)  # fmt: skip
def test_rfc3339_with_any_fraction(raw, micro):
    from news247.market.tape import _rfc3339

    dt = _rfc3339(raw)
    assert dt is not None and dt.microsecond == micro and dt.utcoffset() is not None
