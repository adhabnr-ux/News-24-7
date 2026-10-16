"""Run 2 of the desk: base rates on every alert, confluence alerts, peers and relative strength,
the options-implied move, your stocks in the Brief, the report card, stops/targets/trailing
alerts on tracked calls, and each phone's own severity floor."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from news247.desk import Desk
from news247.dossier import base_line, base_rate, confluence_candidates, peers, record_key, relative_strength
from news247.market.options import Chain, Contract
from news247.market.universe import Universe
from news247.models import Alert, Analysis, NewsItem, Severity, SourceTier
from news247.personal import implied_move, report_card, report_push, straddles, your_stocks, your_stocks_line
from news247.storage import Storage

from .test_desk import DEV, DEV2, engine_with, news
from .test_webpush import Phone

ET = ZoneInfo("America/New_York")
OPEN = datetime(2026, 10, 14, 11, 0, tzinfo=ET).timestamp()  # a Wednesday, mid-session
SEMIS = {
    "ACME": {
        "name": "Acme Semiconductor",
        "mcap": 5e9,
        "price": 100.0,
        "sector": "Technology",
        "industry": "Semiconductors",
    },
    "BETA": {
        "name": "Beta Chips",
        "mcap": 20e9,
        "price": 50.0,
        "sector": "Technology",
        "industry": "Semiconductors",
    },
    "DLTA": {
        "name": "Delta Micro",
        "mcap": 1e9,
        "price": 10.0,
        "sector": "Technology",
        "industry": "Semiconductors",
    },
    "GAMA": {
        "name": "Gamma Health",
        "mcap": 8e9,
        "price": 20.0,
        "sector": "Health Care",
        "industry": "Biotech",
    },
    "SPY": {"name": "SPDR S&P 500 ETF", "mcap": 600e9, "price": 600.0},
}


def semis_engine(tmp_path):
    eng = engine_with(tmp_path)
    uni = Universe.stub(SEMIS)
    eng.universe = uni
    eng.radar.universe = uni
    return eng


def quotes(eng, now, **moves):
    for sym, chg in moves.items():
        eng.live_quotes.update(sym, 100 * (1 + chg / 100), 100.0, now - 10, "tape")


# --------------------------------------------------------------------------- base rates


def test_base_rates_need_enough_graded_alerts():
    rates = {
        "options": {
            "kind": "options",
            "h1": {"n": 20, "hit": 0.6, "median": 1.2},
            "close": {"n": 5, "hit": 0.4, "median": -0.3},
        },
        "news:HIGH": {"kind": "news:HIGH", "h1": {"n": 3, "hit": 1.0, "median": 2.0}, "close": None},
    }
    opt = Alert(kind="options", severity=Severity.HIGH, title="ACME calls", body="", tickers=["ACME"])
    assert record_key(opt) == "options" and record_key(news()) == "news:HIGH"
    b = base_rate(rates, opt)
    assert (
        b["label"] == "Options" and b["h1"]["n"] == 20 and b["close"] is None
    )  # 5 graded is too few to show
    assert base_line(b) == "Alerts like this (Options): 60% went their way in 1h · 20 graded"
    assert base_rate(rates, news()) is None  # 3 graded headlines: not yet
    assert base_line(None) == ""


async def test_each_new_alert_carries_its_base_rate_to_the_push(tmp_path):
    eng = semis_engine(tmp_path)
    eng.base_rates = {
        "news:HIGH": {
            "kind": "news:HIGH",
            "h1": {"n": 12, "hit": 0.75, "median": 1.4},
            "close": {"n": 10, "hit": 0.6, "median": 0.9},
        }
    }
    a = news()
    await eng.publish(a, push=False)
    assert a.edge["base"]["key"] == "news:HIGH"
    assert eng.storage.get_alert(a.id)["edge"]["base"]["h1"]["hit"] == 0.75
    body = eng.webpush.payload(a)["body"]
    assert (
        "Alerts like this (Headlines · HIGH): 75% went their way in 1h, 60% by the close · 12 graded" in body
    )


def test_refresh_base_rates_reads_the_track_record(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    for i in range(3):
        a = news(f"Acme deal {i}")
        a.created = now - 7200 - i
        a.edge = {"outcome": {"ACME": {"h1": 1.0 + i, "close": -1.0, "sign": 1}}}
        eng.storage.add_alert(a)
    eng.refresh_base_rates(now)
    row = eng.base_rates["news:HIGH"]
    assert row["h1"] == {"n": 3, "hit": 1.0, "median": 2.0} and row["close"]["hit"] == 0.0


# --------------------------------------------------------------------------- confluence


def seed_confluence(eng, now):
    a = news("Acme wins $2B Pentagon contract")
    a.created = now - 600
    eng.storage.add_alert(a)
    eng.options_seen["ACME"] = (now - 300, "up", "ACME calls: $4.1M bought at the ask")
    item = NewsItem(source="sec-insider-buys", title="CEO buys $2.1M of Acme (ACME), opportunistic", url="https://sec.test/1",
                    tier=SourceTier.PRIMARY, tickers=["ACME"])  # fmt: skip
    eng.storage.add_item(item, Analysis(score=40, severity=Severity.MEDIUM, tickers=["ACME"]))
    # BETA: a headline and options, but only two kinds of evidence
    b = news("Beta raises guidance", tickers=("BETA",))
    b.created = now - 600
    eng.storage.add_alert(b)
    eng.options_seen["BETA"] = (now - 300, "up", "BETA calls bought")


def test_confluence_needs_three_kinds_on_one_side(tmp_path):
    eng = semis_engine(tmp_path)
    seed_confluence(eng, OPEN)
    got = confluence_candidates(eng, OPEN)
    assert [c["symbol"] for c in got] == ["ACME"]
    c = got[0]
    assert (
        c["side"] == "up"
        and c["kinds"] == ["insider", "news", "options"]
        and c["weight"] == 7.0
        and c["against"] == 0
    )
    # stale: nothing but the insider filing in the last two hours -> not happening now
    assert confluence_candidates(eng, OPEN + 3 * 3600) == []


async def test_confluence_alert_fires_once_and_is_graded(tmp_path):
    eng = semis_engine(tmp_path)
    seed_confluence(eng, OPEN)
    quotes(eng, OPEN, ACME=2.0)
    made = await eng.check_confluence(OPEN)
    assert len(made) == 1
    a = made[0]
    assert a.kind == "confluence" and a.severity == Severity.HIGH and a.tickers == ["ACME"]
    assert a.title.startswith("▲ ACME: insider buying, headlines and options flow line up")
    assert a.edge["refs"] == {"ACME": 102.0} and a.edge["confluence"]["weight"] == 7.0
    assert await eng.check_confluence(OPEN + 60) == []  # once per stock, side and day
    # survives a restart: the day's fired list is saved
    eng.confluence_day = {"day": "", "fired": [], "pushed": 0}
    assert await eng.check_confluence(OPEN + 120) == []
    # its own alert is not evidence for the next scan, and the closed market stays quiet
    night = datetime(2026, 10, 14, 23, 0, tzinfo=ET).timestamp()
    assert await eng.check_confluence(night) == []


def test_confluence_is_its_own_category():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "prefs", {"cats": {"confluence": False}})
    a = Alert(kind="confluence", severity=Severity.HIGH, title="▲ ACME: …", body="", tickers=["ACME"])
    assert d.allows(DEV, a) is False and d.allows(DEV, news()) is True


# --------------------------------------------------------------------------- peers and relative strength


def test_peers_laggards_and_relative_strength(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    quotes(eng, now, ACME=6.0, BETA=0.5, DLTA=0.2, GAMA=-1.0, SPY=0.3)
    p = peers(eng, "acme", now)
    assert p["basis"] == "industry" and p["group"] == "Semiconductors" and p["me"] == 6.0
    assert [r["symbol"] for r in p["rows"]] == [
        "BETA",
        "DLTA",
    ]  # nearest in size first: $20B is 4x away, $1B 5x
    assert {r["symbol"] for r in p["laggards"]} == {"BETA", "DLTA"}  # ACME +6%, they've moved under 1.5%
    rs = relative_strength(eng, "ACME", now)
    assert rs["spy"] == 0.3 and rs["vs_spy"] == 5.7 and rs["sector_name"] == "Technology"
    assert rs["vs_sector"] == pytest.approx(6.0 - (6 * 5 + 0.5 * 20 + 0.2 * 1) / 26, abs=0.01)
    assert rs["read"] == "Its own move"
    quotes(eng, now, ACME=2.0, BETA=2.2, DLTA=1.8)
    assert relative_strength(eng, "ACME", now)["read"] == "Moving with its sector"
    assert peers(eng, "ZZZZ", now)["rows"] == []


def test_ticker_dossier_carries_rel_peers_and_earnings(tmp_path):
    from news247.dossier import ticker_dossier

    eng = semis_engine(tmp_path)
    now = time.time()
    quotes(eng, now, ACME=6.0, BETA=0.5, DLTA=0.2, SPY=0.3)
    eng.trends = SimpleNamespace(
        earnings=SimpleNamespace(
            lookup=lambda s, today=None: (
                {"date": "2026-10-20", "when": "after the close", "in_days": 4} if s == "ACME" else None
            )
        )
    )
    d = ticker_dossier(eng, "ACME", now)
    assert (
        d["rel"]["read"] == "Its own move"
        and d["peers"]["basis"] == "industry"
        and d["earnings"]["in_days"] == 4
    )


# --------------------------------------------------------------------------- the implied move


def chain(today: date) -> Chain:
    e1, e2, far = today + timedelta(days=2), today + timedelta(days=9), today + timedelta(days=90)
    c = [
        Contract("A1C95", "ACME", e1, "C", 95, bid=6.0, ask=6.4),
        Contract("A1C100", "ACME", e1, "C", 100, bid=2.9, ask=3.1),
        Contract("A1P100", "ACME", e1, "P", 100, bid=2.7, ask=2.9),
        Contract("A1P105", "ACME", e1, "P", 105, bid=6.0, ask=6.3),
        Contract("A2C100", "ACME", e2, "C", 100, last=4.0),  # no two-sided quote: the last trade
        Contract("A2P100", "ACME", e2, "P", 100, last=3.5),
        Contract("A3C100", "ACME", far, "C", 100, bid=9.0, ask=9.4),  # past 60 days
        Contract("A3P100", "ACME", far, "P", 100, bid=8.0, ask=8.4),
    ]
    return Chain("ACME", price=100.4, iv30=0.52, contracts=c)


def test_straddles_price_the_move_by_expiry():
    today = date(2026, 10, 14)
    rows = straddles(chain(today), today)
    assert [r["dte"] for r in rows] == [2, 9]
    assert rows[0] == {"expiry": "2026-10-16", "dte": 2, "strike": 100, "straddle": 5.8, "move_pct": 5.78,
                       "low": 94.6, "high": 106.2, "quoted": True}  # fmt: skip
    assert rows[1]["straddle"] == 7.5 and rows[1]["quoted"] is False
    assert straddles(Chain("X"), today) == []  # no price, no answer


async def test_implied_move_reads_the_chain_once_and_flags_earnings(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    today = datetime.fromtimestamp(now, ET).date()
    calls = []

    async def fetch(sym):
        calls.append(sym)
        return chain(today) if sym == "ACME" else None

    assert (await implied_move(eng, "ACME", now))["available"] is False  # no options feed here
    eng.options_feed = SimpleNamespace(fetch_chain=fetch)
    eng.implied_cache.clear()
    exp1 = (today + timedelta(days=2)).isoformat()
    eng.trends = SimpleNamespace(
        earnings=SimpleNamespace(
            lookup=lambda s, t=None: {"date": exp1, "when": "after the close", "in_days": 2}
        )
    )
    out = await implied_move(eng, "ACME", now)
    assert out["available"] and out["iv30"] == 52.0 and [r["earnings"] for r in out["rows"]] == [True, True]
    assert (await implied_move(eng, "ACME", now + 60)) is out and calls == ["ACME"]  # cached 10 minutes
    none = await implied_move(eng, "DLTA", now)
    assert none["available"] is False and none["why"] == "No listed options."


# --------------------------------------------------------------------------- stops, targets, trailing alerts, floors


def test_stops_and_targets_fire_once():
    d = Desk(Storage(":memory:"))
    book = {"ACME": (time.time(), 100.0, 0.0)}
    pf = book.get
    d.apply(
        DEV, "track_add", {"symbol": "ACME", "direction": "long", "stop": 95, "target": "110"}, price_of=pf
    )
    with pytest.raises(ValueError, match="long's stop goes under"):
        d.apply(DEV, "track_add", {"symbol": "ACME", "direction": "long", "stop": 105}, price_of=pf)
    with pytest.raises(ValueError, match="short's stop goes over"):
        d.apply(DEV, "track_add", {"symbol": "ACME", "direction": "short", "stop": 90}, price_of=pf)
    t = d.get(DEV)["tracked"][0]
    assert (t["stop"], t["target"]) == (95.0, 110.0)
    assert d.due_levels(pf) == []
    book["ACME"] = (time.time(), 111.0, 11.0)
    hit = d.due_levels(pf)
    assert [(dev, w) for dev, _, w, _ in hit] == [(DEV, "target")] and d.due_levels(pf) == []
    # moving a level re-arms it; clearing removes it
    d.apply(DEV, "track_levels", {"id": t["id"], "target": 120, "stop": ""})
    assert t["target"] == 120.0 and "target_hit" not in t and t["stop"] is None
    with pytest.raises(ValueError, match="no such"):
        d.apply(DEV, "track_levels", {"id": "nope", "stop": 1})
    # a short: its stop is above
    d.apply(
        DEV2, "track_add", {"symbol": "ACME", "direction": "short", "stop": 115, "target": 100}, price_of=pf
    )
    book["ACME"] = (time.time(), 116.0, 16.0)
    assert [(dev, w) for dev, _, w, _ in d.due_levels(pf)] == [(DEV2, "stop")]


def test_trailing_alert_follows_the_high():
    d = Desk(Storage(":memory:"))
    with pytest.raises(ValueError, match="under 100"):
        d.apply(DEV, "alert_add", {"symbol": "ACME", "kind": "trail", "value": 100})
    d.apply(DEV, "alert_add", {"symbol": "ACME", "kind": "trail", "value": 5})
    fired = []
    for px in (100.0, 108.0, 103.0, 102.0, 90.0):
        fired += [(px, a["peak"]) for _, a, _ in d.due_price_alerts(lambda s, p=px: (time.time(), p, 0.0))]
    assert fired == [(102.0, 108.0)]  # 5% under the 108 high is 102.60


def test_each_phone_sets_its_own_floor_and_old_profiles_get_new_prefs(tmp_path):
    st = Storage(tmp_path / "d.db")
    st.profile_save(
        DEV, {"watchlist": ["ACME"], "prefs": {"cats": {"news": False}, "quiet": None, "watch_push": True}}
    )
    d = Desk(st)
    p = d.get(DEV)["prefs"]
    assert (
        p["min_sev"] is None
        and p["eod"] is True
        and p["cats"]["confluence"] is True
        and p["cats"]["news"] is False
    )
    d.apply(DEV2, "prefs", {"min_sev": "HIGH", "eod": False})
    assert d.get(DEV2)["prefs"]["eod"] is False
    medium = news(sev=Severity.MEDIUM)
    assert d.allows(DEV2, medium) is False and d.allows(DEV2, news(sev=Severity.HIGH)) is True
    assert d.allows(DEV2, Alert(kind="system", severity=Severity.LOW, title="x", body="")) is True
    d.apply(DEV2, "prefs", {"min_sev": "bogus"})
    assert d.get(DEV2)["prefs"]["min_sev"] is None and d.allows(DEV2, medium) is True


# --------------------------------------------------------------------------- your stocks, the report card


def test_your_stocks_and_the_brief_line(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    quotes(eng, now, ACME=6.0, BETA=0.4)
    eng.trends = SimpleNamespace(
        earnings=SimpleNamespace(
            lookup=lambda s, t=None: (
                {"date": "x", "when": "after the close", "in_days": 1} if s == "BETA" else None
            )
        )
    )
    assert your_stocks(eng, DEV, now) == []
    eng.desk.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA", "DLTA"]})
    rows = your_stocks(eng, DEV, now)
    assert [r["symbol"] for r in rows] == ["BETA", "ACME", "DLTA"]  # the report first, then the biggest mover
    assert your_stocks_line(rows) == "★ Your stocks: BETA (reports tomorrow), ACME +6.0%"


def test_report_card(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    quotes(eng, now, ACME=6.0, BETA=-2.0)
    assert report_card(eng, DEV, now) is None
    eng.desk.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA"]})
    eng.desk.apply(DEV, "track_add", {"symbol": "ACME", "direction": "long", "entry": 100}, eng.price_of)
    eng.desk.apply(DEV, "alert_add", {"symbol": "ACME", "kind": "above", "value": 105})
    eng.desk.due_price_alerts(eng.price_of, now)
    a = news()
    a.edge = {"outcome": {"ACME": {"close": 2.0, "sign": 1}}}
    eng.storage.add_alert(a)
    card = report_card(eng, DEV, now)
    assert (
        card["watch"]["up"] == 1
        and card["watch"]["best"]["symbol"] == "ACME"
        and card["watch"]["worst"]["symbol"] == "BETA"
    )
    assert (
        card["tracked"]["avg"] == 6.0
        and len(card["fired"]) == 1
        and card["alerts"] == {"graded": 1, "right": 1}
    )
    push = report_push(card)
    assert push["title"] == "📋 Your day" and push["open"] == "/app/#a=desk"
    assert push["body"].splitlines() == [
        "Watchlist: 1 of 2 up · best ACME +6.0% · worst BETA -2.0%",
        "Tracked calls: 1 open, avg +6.0%",
        "Fired today: 1 price alert(s), 0 stop/target(s)",
        "Foretape on your stocks: 1 of 1 went the alert's way by the close",
    ]


def test_report_card_window(tmp_path):
    eng = semis_engine(tmp_path)
    at = lambda h, m, d=14: datetime(2026, 10, d, h, m, tzinfo=ET).timestamp()  # noqa: E731
    assert eng._eod_due(at(16, 10)) and not eng._eod_due(at(15, 59)) and not eng._eod_due(at(18, 0))
    assert not eng._eod_due(at(16, 10, d=17))  # Saturday
    eng.storage.set_setting("eod_sent", "2026-10-14")
    assert not eng._eod_due(at(16, 30))


# --------------------------------------------------------------------------- pushed to the right phone


async def test_personal_pushes_reach_only_their_phone(tmp_path, server, monkeypatch):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, b = Phone(server.url("/push/a")), Phone(server.url("/push/b"))
        sa, sb = a.subscription(), b.subscription()
        sa.device, sb.device = DEV, DEV2
        eng.webpush.subs = {sa.endpoint: sa, sb.endpoint: sb}  # type: ignore[union-attr]
        now = time.time()
        quotes(eng, now, ACME=6.0)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        # the Brief: A's copy names its stocks, B's doesn't
        monkeypatch.setattr(
            "news247.engine.brief_push",
            lambda b: {
                "id": "brief",
                "title": "☀️ The Brief",
                "body": "2 overnight catalysts",
                "tag": "brief",
            },
        )
        eng.brief = lambda now=None: {}
        assert await eng.send_brief(now)
        got = {
            r["path"]: (a if r["path"] == "/push/a" else b).read(r["body"])["body"] for r in server.requests
        }
        assert got == {
            "/push/a": "2 overnight catalysts\n★ Your stocks: ACME +6.0%",
            "/push/b": "2 overnight catalysts",
        }
        # the report card goes to A only (B has no desk)
        server.requests.clear()
        assert await eng.send_report_cards(now) == 1
        assert [r["path"] for r in server.requests] == ["/push/a"]
        # a target reached: A's phone, once
        eng.desk.apply(
            DEV,
            "track_add",
            {"symbol": "ACME", "direction": "long", "entry": 100, "target": 105},
            eng.price_of,
        )
        server.requests.clear()
        await eng.check_price_alerts(now)
        await eng.check_price_alerts(now + 15)
        assert [r["path"] for r in server.requests] == ["/push/a"]
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "🎯 ACME hit your target ($105.00)" and msg["body"].startswith(
            "Long from $100.00, now $106.00 · +6.00%"
        )
    finally:
        await eng.http.close()


async def test_run2_over_the_api(tmp_path):
    import aiohttp
    from aiohttp.test_utils import TestServer

    from news247.web.server import WebServer

    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            quotes(eng, time.time(), ACME=6.0, BETA=0.5, DLTA=0.1)
            async with s.post(
                srv.make_url("/api/desk"), json={"action": "watch_add", "symbol": "ACME"}, headers=h
            ) as r:
                assert r.status == 200
            async with s.get(srv.make_url("/api/implied/acme"), headers=h) as r:
                assert (await r.json())["available"] is False
            async with s.get(srv.make_url("/api/peers/ACME"), headers=h) as r:
                assert {x["symbol"] for x in (await r.json())["laggards"]} == {"BETA", "DLTA"}
            async with s.get(srv.make_url("/api/report"), headers=h) as r:
                assert (await r.json())["watch"]["best"]["symbol"] == "ACME"
            async with s.get(srv.make_url("/api/brief"), headers=h) as r:
                assert (await r.json())["mine"][0]["symbol"] == "ACME"
            async with s.get(srv.make_url("/api/report"), headers={"Authorization": "Bearer tok"}) as r:
                assert r.status == 400
            async with s.post(
                srv.make_url("/api/desk"), json={"action": "prefs", "min_sev": "CRITICAL"}, headers=h
            ) as r:
                assert (await r.json())["prefs"]["min_sev"] == "CRITICAL"
    finally:
        await srv.close()
        await eng.http.close()
