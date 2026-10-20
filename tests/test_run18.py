"""Run 18: the edge finder (kinds of news x size x exit, ranked) and the paper autopilot (forward-
testing a rule on new alerts)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.autopilot import clean_rule, due_exit, exit_time, matches, rule_label
from news247.catalysts import edge_finder
from news247.dossier import strategy_lab
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import semis_engine
from .test_run3 import phones
from .test_run15 import graded
from .test_run16 import contract_history, stored


def history(eng):
    for i, r in enumerate([3.0, 2.0, 4.0, 1.0, 2.5, 3.5, 2.0, 1.5]):  # contracts: a steady edge
        eng.storage.add_alert(graded(f"Acme wins contract number {i}", r))
    for i, r in enumerate([-2.0, -1.0, 0.5, -3.0, -1.5, -2.5, -1.0, -0.5]):  # upgrades: fade
        eng.storage.add_alert(graded(f"Morgan Stanley upgrades Acme {i}", r))
    for i, r in enumerate([5.0, -4.0, 6.0]):  # too few to judge
        eng.storage.add_alert(graded(f"FDA approves Acme drug {i}", r))


def test_the_edge_finder_ranks_kinds_of_news(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    e = edge_finder(eng)
    best = e["best"][0]
    assert (best["catalyst"], best["horizon"]) == ("contract", "close") and best["n"] == 8
    assert best["hit"] == 1.0 and best["avg"] == pytest.approx(2.44, abs=0.01) and best["t"] > 5
    assert (
        best["score"] == pytest.approx(2.44 * 8 / 18, abs=0.01)
        and best["horizon_label"] == "out at the close"
    )
    assert {(r["catalyst"], r["band"]) for r in e["best"] if r["horizon"] == "close"} == {("contract", "mid")}
    worst = e["worst"][0]
    assert worst["catalyst"] == "upgrade" and worst["avg"] < 0 and worst["hit"] == 0.12
    assert not any(r["catalyst"] == "fda_approval" for r in e["best"] + e["worst"])  # 3 graded: under min_n
    assert edge_finder(eng, min_n=3)["combos"] > e["combos"]


def test_the_lab_filters_by_kind_of_news_and_size(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    lab = strategy_lab(eng, catalyst="contract")
    assert lab["n"] == 8 and lab["hit"] == 1.0 and lab["catalyst"] == "contract"
    assert strategy_lab(eng, catalyst="contract", band="large")["n"] == 0  # ACME is mid
    assert strategy_lab(eng, catalyst="upgrade")["pnl"] < 0
    assert strategy_lab(eng)["n"] == 19
    for bad in ({"catalyst": "gossip"}, {"band": "huge"}):
        with pytest.raises(ValueError):
            strategy_lab(eng, **bad)


# --------------------------------------------------------------------------- the autopilot


def test_rules_are_checked_and_matched():
    r = clean_rule({"catalyst": "contract", "band": "mid", "min_sev": "high", "exit": "close"}, 0)
    assert r["label"] == "Contract win, $2-10B, HIGH+, out at the close" and r["on"] is True
    assert (
        rule_label({**r, "catalyst": "all", "band": "all", "exit": "d1"})
        == "Any kind of news, any size, HIGH+, out at the next day's close"
    )
    for bad in ({"catalyst": "gossip"}, {"band": "huge"}, {"min_sev": "LOW"}, {"exit": "week"}):
        with pytest.raises(ValueError):
            clean_rule(bad, 0)
    a = {"kind": "news", "severity": "HIGH", "edge": {"catalyst": {"key": "contract", "band": "mid"}},
         "analysis": {"direction": "up"}}  # fmt: skip
    assert matches(r, a)
    assert not matches(r, {**a, "severity": "MEDIUM"})
    assert not matches(r, {**a, "analysis": {"direction": "mixed"}})
    assert not matches(r, {**a, "edge": {"catalyst": {"key": "upgrade", "band": "mid"}}})
    assert not matches(r, {**a, "edge": {"catalyst": {"key": "contract", "band": "large"}}})
    assert not matches(r, {**a, "edge": {}})  # news it couldn't read for a kind
    assert not matches({**r, "on": False}, a)
    t = {"direction": "long", "stop": 99.0, "target": 103.0, "exit_at": 1000.0}
    assert (
        due_exit(t, 98.5, 0),
        due_exit(t, 103.5, 0),
        due_exit(t, 101.0, 0),
        due_exit(t, 101.0, 1000.0),
    ) == ("stop", "target", "", "time")
    s = {**t, "direction": "short", "stop": 103.0, "target": 99.0}
    assert (due_exit(s, 103.5, 0), due_exit(s, 98.0, 0)) == ("stop", "target")


def test_exit_times():
    cal = SimpleNamespace(holidays=set(), early=set())
    from .test_run13 import at

    assert exit_time("h1", at(10, 0), cal) == at(11, 0)
    assert exit_time("close", at(10, 0), cal) == at(16, 0)
    assert exit_time("d1", at(10, 0), cal) == at(16, 0, d=15)
    assert exit_time("close", at(17, 0), cal) == at(16, 0, d=15)  # after hours: the next session


async def test_the_autopilot_opens_and_closes_paper_calls(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    contract_history(eng)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        now = time.time()
        eng.live_quotes.update("ACME", 100.5, 99.0, now - 5, "tape")
        eng.desk.apply(
            DEV, "autopilot_add", {"catalyst": "contract", "band": "all", "min_sev": "HIGH", "exit": "h1"}
        )
        with pytest.raises(ValueError, match="already running"):
            eng.desk.apply(
                DEV, "autopilot_add", {"catalyst": "contract", "band": "all", "min_sev": "HIGH", "exit": "h1"}
            )
        rule = eng.desk.get(DEV)["autopilots"][0]
        alert = stored(eng)
        alert["edge"]["catalyst"] = {"key": "contract", "label": "Contract win", "band": "mid"}
        assert await eng.autopilot_open(alert, now) == 1
        t = eng.desk.get(DEV)["tracked"][0]
        assert (t["symbol"], t["direction"], t["auto"], t["tags"]) == (
            "ACME",
            "long",
            rule["id"],
            ["autopilot"],
        )
        assert t["stop"] == pytest.approx(99.0099, abs=1e-3) and t["target"] == pytest.approx(102.75)
        assert t["exit_at"] == pytest.approx(now + 3600)
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "🤖 Autopilot: long ACME at $100.50" and "out an hour later" in msg["body"]
        assert eng.desk.get(DEV2)["tracked"] == []  # only the phone with the rule
        # three a rule a day
        for _ in range(4):
            await eng.autopilot_open(alert, now)
        assert len(eng.desk.get(DEV)["tracked"]) == 3
        # the target is reached: that one closes, with a push; the others wait for their time
        eng.live_quotes.update("ACME", 103.0, 99.0, now + 60, "tape")
        assert await eng.run_autopilots(now + 60) == 3  # all three share the target
        closed = eng.desk.get(DEV)["closed"]
        assert closed[0]["lesson"] == "autopilot: target reached" and closed[0]["pnl_pct"] == pytest.approx(
            2.49, abs=0.01
        )
        assert a.read(server.requests[-1]["body"])["title"] == "🤖 Closed long ACME +2.49%"
        stats = eng.desk.view(DEV, eng.price_of)["autopilots"][0]["stats"]
        assert (stats["open"], stats["closed"], stats["win"]) == (0, 3, 1.0)
        # a new day: one more, closed by time
        later = now + 86400
        eng.live_quotes.update("ACME", 100.5, 99.0, later, "tape")
        assert await eng.autopilot_open(alert, later) == 1
        eng.live_quotes.update("ACME", 101.0, 99.0, later + 3700, "tape")
        assert await eng.run_autopilots(later + 3700) == 1
        assert eng.desk.get(DEV)["closed"][0]["lesson"] == "autopilot: out an hour later"
        # off: nothing opens; removed: gone
        eng.desk.apply(DEV, "autopilot_toggle", {"id": rule["id"]})
        assert await eng.autopilot_open(alert, later + 86400) == 0
        eng.desk.apply(DEV, "autopilot_remove", {"id": rule["id"]})
        assert eng.desk.get(DEV)["autopilots"] == []
        with pytest.raises(ValueError):
            eng.desk.apply(DEV, "autopilot_remove", {"id": rule["id"]})
    finally:
        await eng.http.close()


async def test_run18_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/edges"), headers=h) as r:
                assert (await r.json())["best"][0]["catalyst"] == "contract"
            async with s.get(srv.make_url("/api/lab?catalyst=contract&band=mid&sev=HIGH"), headers=h) as r:
                assert (await r.json())["n"] == 8
            async with s.get(srv.make_url("/api/lab?catalyst=gossip"), headers=h) as r:
                assert r.status == 400
            async with s.post(
                srv.make_url("/api/desk"),
                json={"action": "autopilot_add", "catalyst": "contract", "exit": "close"},
                headers=h,
            ) as r:
                ap = (await r.json())["autopilots"][0]
                assert (
                    ap["label"] == "Contract win, any size, HIGH+, out at the close"
                    and ap["stats"]["open"] == 0
                )
            async with s.post(
                srv.make_url("/api/desk"), json={"action": "autopilot_add", "catalyst": "gossip"}, headers=h
            ) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()


def test_autopilots_only_take_news_with_a_kind(tmp_path):
    eng = semis_engine(tmp_path)
    a = news("Acme hosts investor day")
    eng.desk.apply(DEV, "autopilot_add", {"catalyst": "all", "exit": "close"})
    assert not matches(eng.desk.get(DEV)["autopilots"][0], a.to_dict())
