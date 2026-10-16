"""Run 8 of the desk: smart alerts (a phone's own combined triggers), stocks in play, the market
regime, the close, shared watchlists, similar past alerts and the story timeline."""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.desk import Desk
from news247.dossier import close_push, close_recap, in_play, regime, similar_alerts
from news247.market.universe import Universe
from news247.models import Alert, Analysis, Severity
from news247.storage import Storage
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import quotes, semis_engine
from .test_run3 import item, phones

ET = ZoneInfo("America/New_York")


def at(h, m, d=14):
    return datetime(2026, 10, d, h, m, tzinfo=ET).timestamp()


# --------------------------------------------------------------------------- smart alerts


def test_rules_are_validated():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "rule_add", {"kind": "headline", "symbol": "acme"})
    d.apply(DEV, "rule_add", {"kind": "news_move", "symbol": "*", "value": 3})
    d.apply(DEV, "rule_add", {"kind": "vwap", "symbol": "ACME", "dir": "above"})
    assert [(r["kind"], r["symbol"], r["value"]) for r in d.get(DEV)["rules"]] == [
        ("headline", "ACME", 20.0),
        ("news_move", "*", 3.0),
        ("vwap", "ACME", 0.0),
    ]
    for bad in ({"kind": "moon", "symbol": "ACME"}, {"kind": "volume", "symbol": "ACME", "value": 1}, {"kind": "vwap", "symbol": "ACME"},
                {"kind": "gap", "symbol": "!!", "value": 3}):  # fmt: skip
        with pytest.raises(ValueError):
            d.apply(DEV, "rule_add", bad)
    rid = d.get(DEV)["rules"][0]["id"]
    d.apply(DEV, "rule_remove", {"id": rid})
    assert len(d.view(DEV, lambda s: None)["rules"]) == 2


async def test_rules_fire_once_on_their_phone(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        now = time.time()
        eng.live_quotes.update("ACME", 100.0, 100.0, now - 600, "tape")
        it = item("Acme wins $2B Pentagon contract")
        it.detected = now - 590
        eng.storage.add_item(it, Analysis(score=40, severity=Severity.MEDIUM, tickers=["ACME"]))
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        eng.desk.apply(DEV, "rule_add", {"kind": "news_move", "symbol": "*", "value": 3})
        eng.desk.apply(DEV, "rule_add", {"kind": "headline", "symbol": "ACME", "value": 30})
        assert await eng.check_rules(now) == 1  # the headline rule; no move yet
        assert a.read(server.requests[0]["body"])["title"] == "📰 ACME: Acme wins $2B Pentagon contract"
        eng.live_quotes.update("ACME", 104.0, 100.0, now + 60, "tape")  # +4% since the headline's price
        server.requests.clear()
        assert await eng.check_rules(now + 70) == 1
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "⚡ ACME +4.0% since the headline" and msg["open"] == "/app/#t=ACME"
        assert await eng.check_rules(now + 90) == 0  # once per headline
    finally:
        await eng.http.close()


async def test_volume_vwap_and_gap_rules(tmp_path):
    eng = semis_engine(tmp_path)
    reads = {"ACME": {"rvol": 6.0, "chg": 3.0, "price": 103.0, "vwap": 104.0}}
    eng.ignition = SimpleNamespace(latest=lambda s, now=None: reads.get(s))
    eng.desk.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA"]})
    for body in (
        {"kind": "volume", "symbol": "ACME", "value": 5},
        {"kind": "vwap", "symbol": "ACME", "dir": "above"},
        {"kind": "gap", "symbol": "*", "value": 4},
    ):
        eng.desk.apply(DEV, "rule_add", body)
    pre = at(8, 30)
    quotes(eng, pre, BETA=-5.0)
    sent = []

    async def to_device(device, payload, urgency="high"):
        sent.append(payload["title"])
        return True

    eng._to_device = to_device
    await eng.check_rules(pre)
    assert sorted(sent) == [
        "🌅 BETA gapping -5.0% pre-market",
        "📊 ACME at 6.0× its normal volume",
    ]  # VWAP: first read sets the side
    reads["ACME"]["price"] = 105.0
    sent.clear()
    await eng.check_rules(pre + 30)
    assert sent == ["〽️ ACME crossed above VWAP"]


# --------------------------------------------------------------------------- in play, regime, the close


def big_universe():
    return Universe.stub({
        "TECH1": {"name": "Tech One", "mcap": 50e9, "sector": "Technology"}, "TECH2": {"name": "Tech Two", "mcap": 40e9, "sector": "Technology"},
        "TECH3": {"name": "Tech Three", "mcap": 30e9, "sector": "Technology"}, "UTIL1": {"name": "Util One", "mcap": 50e9, "sector": "Utilities"},
        "UTIL2": {"name": "Util Two", "mcap": 40e9, "sector": "Utilities"}, "UTIL3": {"name": "Util Three", "mcap": 30e9, "sector": "Utilities"},
        "SMOL": {"name": "Small Co", "mcap": 1e9, "sector": "Technology"},
    })  # fmt: skip


def test_regime_reads_cyclicals_breadth_and_size(tmp_path):
    eng = semis_engine(tmp_path)
    eng.universe = big_universe()
    now = time.time()
    assert regime(eng, now)["label"] == "unknown"
    quotes(eng, now, TECH1=2.0, TECH2=1.5, TECH3=1.0, UTIL1=-0.5, UTIL2=0.2, UTIL3=0.1, SMOL=3.0)
    r = regime(eng, now)
    assert (
        r["label"] == "risk-on"
        and r["inputs"]["cyc_minus_def"] > 0.5
        and r["why"][0].startswith("cyclicals ahead of defensives")
    )
    quotes(eng, now, TECH1=-2.0, TECH2=-1.5, TECH3=-1.0, UTIL1=0.5, UTIL2=-0.2, UTIL3=0.3, SMOL=-3.0)
    assert regime(eng, now)["label"] == "risk-off"


def test_in_play_merges_the_boards(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    quotes(eng, now, ACME=7.0, BETA=-4.0, DLTA=0.5)
    eng.ignition = SimpleNamespace(volume_board=lambda now, m: [{"symbol": "DLTA", "rvol": 9.0}])
    eng.radar.board["ACME"] = {"symbol": "ACME", "flagged": True}
    conf = Alert(
        kind="confluence",
        severity=Severity.HIGH,
        title="▲ ACME: …",
        body="",
        tickers=["ACME"],
        created=now - 60,
    )
    eng.storage.add_alert(conf)
    rows = in_play(eng, now)["rows"]
    assert [r["symbol"] for r in rows][0] == "ACME" and set(rows[0]["why"]) == {
        "+7.0% today",
        "on the radar",
        "confluence alert",
    }
    assert {r["symbol"] for r in rows} == {"ACME", "BETA", "DLTA"} and next(
        r for r in rows if r["symbol"] == "DLTA"
    )["why"] == ["9× normal volume"]


def test_the_close(tmp_path):
    eng = semis_engine(tmp_path)
    eng.universe = big_universe()
    now = at(16, 20)
    quotes(eng, now, TECH1=2.0, TECH2=1.5, TECH3=4.0, UTIL1=-0.5, UTIL2=0.2, UTIL3=0.1, SPY=0.8, QQQ=1.2)
    c = close_recap(eng, now)
    assert (
        c["indexes"] == {"SPY": 0.8, "QQQ": 1.2}
        and c["best"][0]["sector"] == "Technology"
        and c["up"][0]["symbol"] == "TECH3"
    )
    p = close_push(c)
    assert (
        p["title"] == "🔔 The close"
        and p["body"].splitlines()[0] == "SPY +0.8% · QQQ +1.2%"
        and p["open"] == "/app/#a=brief"
    )
    assert eng._close_due(now) and not eng._close_due(at(15, 0)) and not eng._close_due(at(16, 20, d=17))


async def test_the_close_goes_only_to_phones_that_asked(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        phones(eng, server)
        now = at(16, 20)
        quotes(eng, now, SPY=0.8)
        assert await eng.send_close(now) == 0
        eng.desk.apply(DEV2, "prefs", {"close_recap": True})
        eng.storage.set_setting("close_sent", "")
        assert await eng.send_close(now) == 1 and [r["path"] for r in server.requests] == ["/push/b"]
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- shared lists, similar alerts, the story


def test_a_watchlist_can_be_shared_by_code():
    st = Storage(":memory:")
    d = Desk(st)
    with pytest.raises(ValueError, match="empty"):
        d.apply(DEV, "share_list", {})
    d.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA"]})
    d.apply(DEV, "share_list", {"name": "Semis I like"})
    code = d.view(DEV, lambda s: None)["share"]["code"]
    d.apply(DEV2, "watch_add", {"symbol": "GAMA"})
    for _ in range(2):  # any number of uses
        d.apply(DEV2, "import_list", {"code": code[:4] + "-" + code[4:]})
    p2 = d.get(DEV2)
    assert p2["watchlist"] == ["GAMA", "ACME", "BETA"] and p2["groups"] == {
        "ACME": "Semis I like",
        "BETA": "Semis I like",
    }
    with pytest.raises(ValueError, match="wrong or expired"):
        d.apply(DEV2, "import_list", {"code": "NOPE1234"})


def test_similar_past_alerts_on_the_stock(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    for i, close in enumerate([2.0, -1.0, 3.0]):
        a = news(f"Acme deal {i}")
        a.created = now - 86400 * (i + 1)
        a.edge = {"outcome": {"ACME": {"close": close, "sign": 1}}}
        eng.storage.add_alert(a)
    other = news("Acme options", sev=Severity.MEDIUM)  # another kind: not similar
    other.created = now - 3600
    eng.storage.add_alert(other)
    cur = news("Acme new deal")
    cur.created = now
    eng.storage.add_alert(cur)
    s = similar_alerts(eng, eng.storage.get_alert(cur.id))
    assert len(s["rows"]) == 3 and s["stats"] == {"n": 3, "hit": 0.67, "median": 2.0}


async def test_story_timeline_and_run8_api(tmp_path):
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        first = item("Acme wins $2B Pentagon contract")
        eng.storage.add_item(first, Analysis(score=60, severity=Severity.HIGH, tickers=["ACME"]), story_id=7)
        second = item("Pentagon awards Acme $2B deal", source="gnews")
        second.detected = first.detected + 90
        eng.storage.add_item(
            second, Analysis(score=40, severity=Severity.MEDIUM, tickers=["ACME"]), story_id=7
        )
        a = news()
        a.item = first
        eng.storage.add_alert(a)
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url(f"/api/alert/{a.id}"), headers=h) as r:
                body = await r.json()
                assert [x["source"] for x in body["story_items"]] == ["globenewswire", "gnews"] and body[
                    "story_items"
                ][1]["score"] == 40
            for path in ("/api/inplay", "/api/regime", "/api/close", f"/api/similar/{a.id}"):
                async with s.get(srv.make_url(path), headers=h) as r:
                    assert r.status == 200, path
            async with s.get(srv.make_url("/api/similar/nope"), headers=h) as r:
                assert r.status == 404
    finally:
        await srv.close()
        await eng.http.close()
