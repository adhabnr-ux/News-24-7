"""Run 17: catalyst controls (turn kinds of news off; an edge bar on their record) and analyst
price targets read from the headlines."""

from __future__ import annotations

import time

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.catalysts import catalyst_kinds, catalyst_taste
from news247.desk import Desk, edge_miss
from news247.models import Analysis, Severity
from news247.pushcheck import would_push
from news247.storage import Storage
from news247.targets import is_big, parse_target, target_board, targets_for
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import quotes, semis_engine
from .test_run3 import item, phones
from .test_run15 import graded


def with_catalyst(a, key="upgrade", label="Analyst upgrade", base=None):
    a.edge = {"catalyst": {"key": key, "label": label, "band": "mid"}, **({"cat_base": base} if base else {})}
    return a


# --------------------------------------------------------------------------- catalyst controls


def test_kinds_of_news_can_be_turned_off():
    d = Desk(Storage(":memory:"))
    up = with_catalyst(news("Morgan Stanley upgrades Acme"))
    assert d.allows(DEV, up) is True
    d.apply(DEV, "cat_mute", {"key": "upgrade"})
    b = d.block(DEV, up)
    assert b["code"] == "catalyst_off" and b["text"] == "you turned off analyst upgrade news"
    assert b["fix"] == {
        "label": "Push analyst upgrade news again",
        "action": "cat_mute",
        "body": {"key": "upgrade", "on": False},
    }
    assert d.allows(DEV, with_catalyst(news("Acme wins contract"), "contract", "Contract win")) is True
    crit = with_catalyst(news("Morgan Stanley upgrades Acme", sev=Severity.CRITICAL))
    assert d.allows(DEV, crit) is True  # CRITICAL still comes
    d.apply(DEV, "stock_push", {"symbol": "ACME", "level": "all"})
    assert d.allows(DEV, up) is True  # a stock set to everything wins
    d.apply(DEV, "stock_push", {"symbol": "ACME", "level": "default"})
    d.apply(DEV, b["fix"]["action"], b["fix"]["body"])
    assert d.get(DEV)["cat_off"] == [] and d.allows(DEV, up) is True
    with pytest.raises(ValueError):
        d.apply(DEV, "cat_mute", {"key": "gossip"})


def test_the_edge_bar_holds_back_news_with_a_poor_record():
    d = Desk(Storage(":memory:"))
    weak = {"close": {"n": 20, "hit": 0.45, "median": -0.2}, "typical": 1.2}
    strong = {"close": {"n": 20, "hit": 0.65, "median": 1.4}, "typical": 3.1}
    assert edge_miss(weak, "hit50") == "45% its way, under 50%" and edge_miss(strong, "hit60") == ""
    assert edge_miss(weak, "move2") == "typical move ±1.2%, under 2%" and edge_miss(None, "hit60") == ""
    d.apply(DEV, "prefs", {"edge_min": "hit60"})
    b = d.block(DEV, with_catalyst(news("Upgrade"), base=weak))
    assert (
        b["code"] == "edge"
        and "45% its way, under 60%" in b["text"]
        and b["fix"]["body"] == {"edge_min": None}
    )
    assert d.allows(DEV, with_catalyst(news("Upgrade"), base=strong)) is True
    assert d.allows(DEV, with_catalyst(news("Upgrade"))) is True  # no record yet: not held back
    assert d.allows(DEV, news("No catalyst read")) is True  # not news Foretape could read: not judged
    with pytest.raises(ValueError):
        d.apply(DEV, "prefs", {"edge_min": "hit99"})
    d.apply(DEV, "prefs", {"edge_min": ""})
    assert d.get(DEV)["prefs"]["edge_min"] is None


async def test_what_if_takes_a_kind_of_news_and_votes_suggest_turning_one_off(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    for i, r in enumerate([-1.0, 2.0, -3.0, 1.0, -2.5, -0.5]):
        eng.storage.add_alert(graded(f"Morgan Stanley upgrades Acme {i}", r))
    eng.refresh_base_rates()
    await eng.http.start()
    try:
        phones(eng, server)
        assert would_push(eng, DEV, "news", "HIGH", "ACME", catalyst="upgrade")["got"] is True
        eng.desk.apply(DEV, "prefs", {"edge_min": "hit50"})
        w = would_push(eng, DEV, "news", "HIGH", "ACME", catalyst="upgrade")
        assert w["got"] is False and "under your bar (33% its way, under 50%)" in w["verdict"]
        assert w["title"] == "A HIGH analyst upgrade alert on ACME"
        with pytest.raises(ValueError):
            would_push(eng, DEV, "news", "HIGH", "ACME", catalyst="gossip")
        ids = [a["id"] for a in eng.storage.recent_alerts(10)]
        for aid in ids[:3]:
            eng.desk.apply(DEV, "vote", {"id": aid, "v": -1})
        taste = catalyst_taste(eng, DEV)
        assert taste == [
            {"key": "upgrade", "label": "Analyst upgrade", "up": 0, "down": 3, "suggest_off": True}
        ]
        eng.desk.apply(DEV, "cat_mute", {"key": "upgrade"})
        assert catalyst_taste(eng, DEV)[0]["suggest_off"] is False  # already off
        kinds = {k["key"]: k for k in catalyst_kinds(eng.catalyst_rates)}
        assert kinds["upgrade"]["close"]["n"] == 6 and kinds["fda_approval"]["close"] is None
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- analyst price targets


@pytest.mark.parametrize(
    "title,firm,action,to,frm,rating",
    [
        (
            "Goldman Sachs raises Acme price target to $150 from $120",
            "Goldman Sachs",
            "raise",
            150.0,
            120.0,
            "",
        ),
        ("Acme PT lowered to $80 at Morgan Stanley", "Morgan Stanley", "cut", 80.0, None, ""),
        ("Jefferies initiates Acme at buy with $60 target", "Jefferies", "initiate", 60.0, None, "buy"),
        (
            "Morgan Stanley Maintains Overweight on Acme, Raises Price Target to $150",
            "Morgan Stanley",
            "raise",
            150.0,
            None,
            "overweight",
        ),
        (
            "Acme price target cut to $45 from $60 by Piper Sandler; rating neutral",
            "Piper Sandler",
            "cut",
            45.0,
            60.0,
            "neutral",
        ),
        ("Wedbush reiterates outperform, $200 PT on Acme", "Wedbush", "maintain", 200.0, None, "outperform"),
        ("Citi lifts Acme target to $1,250.50", "Citi", "raise", 1250.5, None, ""),
        ("Analyst sets $1,200 price target on Acme", "", "set", 1200.0, None, ""),
    ],
)
def test_target_headlines_are_read(title, firm, action, to, frm, rating):
    pt = parse_target(title)
    assert (pt["firm"], pt["action"], pt["to"], pt["from"], pt["rating"]) == (firm, action, to, frm, rating)


def test_headlines_that_are_not_targets_are_skipped():
    for t in ("Acme stock hits target of $300 million in sales", "Acme targets $2B in revenue",
              "Acme to buy Beta for $40 a share", "Acme raises $500M, targets $1.2B valuation",
              "Acme reaches its sales target", "Goldman raises Acme rating to buy"):  # fmt: skip
        assert parse_target(t) is None, t


def store(eng, title, sym="ACME", ago=3600):
    it = item(title, tickers=(sym,))
    it.detected = it.published = time.time() - ago
    eng.storage.add_item(it, Analysis(score=40, severity=Severity.MEDIUM, tickers=[sym]))


def test_a_stocks_targets_and_the_board(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    quotes(eng, now, ACME=0.0, BETA=0.0)  # both at $100
    store(eng, "Goldman Sachs raises Acme price target to $150 from $120", ago=3600)
    store(eng, "Goldman Sachs raises Acme price target to $120 from $110", ago=86400 * 10)  # older: replaced
    store(eng, "Acme price target cut to $90 from $100 by Piper Sandler", ago=7200)
    store(eng, "Jefferies initiates Acme at buy with $130 target", ago=86400 * 40)
    store(eng, "Morgan Stanley cuts Beta price target to $60 from $100", sym="BETA", ago=600)
    t = targets_for(eng, "ACME", now)
    assert [(r["firm"], r["to"]) for r in t["rows"]] == [
        ("Goldman Sachs", 150.0),
        ("Piper Sandler", 90.0),
        ("Jefferies", 130.0),
    ]
    assert (t["median"], t["high"], t["low"], t["upside"]) == (130.0, 150.0, 90.0, 30.0)
    assert (t["raises"], t["cuts"]) == (2, 1) and t["rows"][0]["upside"] == 50.0 and len(t["history"]) == 4
    assert targets_for(eng, "GAMA", now)["n"] == 0
    board = target_board(eng, now)
    assert [(r["symbol"], r["change_pct"]) for r in board["rows"][:2]] == [("BETA", -40.0), ("ACME", 25.0)]


async def test_watchers_get_big_target_moves(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        quotes(eng, time.time(), ACME=8.5)  # $108.50
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        assert await eng._target_push(item("Goldman Sachs raises Acme price target to $150 from $120")) == 1
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "🎯 Goldman Sachs raises ACME target to $150"
        assert (
            msg["body"].startswith("+25% from $120 · +38% from the price ($108.50)")
            and msg["open"] == "/app/#t=ACME"
        )
        assert await eng._target_push(item("Citi lifts Acme target to $112 from $110")) == 0  # small: no push
        assert await eng._target_push(item("Acme hosts an investor day")) == 0
        eng.desk.apply(DEV, "mute", {"symbol": "ACME", "hours": 1})
        assert await eng._target_push(item("Barclays cuts Acme price target to $70 from $100")) == 0  # muted
    finally:
        await eng.http.close()
    assert is_big({"change_pct": None, "to": 140.0, "action": "initiate"}, 100.0) is True
    assert is_big({"change_pct": None, "to": 110.0, "action": "initiate"}, 100.0) is False
    assert is_big({"change_pct": None, "to": 150.0, "action": "maintain"}, 100.0) is False


async def test_run17_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    quotes(eng, time.time(), ACME=0.0)
    store(eng, "Goldman Sachs raises Acme price target to $150 from $120")
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV2}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/targets/acme"), headers=h) as r:
                assert (await r.json())["median"] == 150.0
            async with s.get(srv.make_url("/api/targets/%3F%3F"), headers=h) as r:
                assert r.status == 400
            async with s.get(srv.make_url("/api/targets"), headers=h) as r:
                assert (await r.json())["rows"][0]["firm"] == "Goldman Sachs"
            async with s.post(
                srv.make_url("/api/desk"), json={"action": "cat_mute", "key": "downgrade"}, headers=h
            ) as r:
                body = await r.json()
                assert body["cat_off"] == ["downgrade"] and any(
                    k["key"] == "downgrade" for k in body["catalyst_kinds"]
                )
                assert body["catalyst_taste"] == []
            async with s.post(
                srv.make_url("/api/would-push"),
                json={"category": "news", "catalyst": "downgrade", "symbol": "ACME"},
                headers=h,
            ) as r:
                assert (await r.json())["got"] is False
    finally:
        await srv.close()
        await eng.http.close()
