"""Run 61: where to put the stop for each kind of news (every stop, priced side by side on the same
alerts, with how often each one shook out a winner) and what the record learned this week (the
verdicts that changed since a week ago, in a Friday push and on the Watch tab)."""

from __future__ import annotations

import json
import time

import aiohttp
from aiohttp.test_utils import TestServer

from news247.learned import diff, learned, previous, push, save_snapshot, snapshot
from news247.stops_kind import replay, stop_line, stop_price, stop_rates, stop_widths
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones
from .test_run16 import contract_history
from .test_run60 import et, rec

# --------------------------------------------------------------------------- where to put the stop


def graded(close, mae, sign=1, plan=None):
    return {"edge": {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": sign, "close": close}},
                     "exc": {"mae": mae, "mfe": 3.0}, **({"plan": plan} if plan else {})}}  # fmt: skip


def test_one_alert_replayed_with_every_stop():
    a = graded(2.0, -1.5, plan={"entry": 100.0, "stop": 97.5, "target": 104.0})
    r = replay(a, {"dip80": 0.9})
    assert r["none"] == {"r": 2.0, "stopped": False, "won": True}
    assert r["w1"] == {"r": -1.0, "stopped": True, "won": True}  # cut a winner: shaken out
    assert r["w2"]["r"] == 2.0 and not r["w2"]["stopped"]
    assert r["heat"] == {"r": -1.05, "stopped": True, "won": True}  # 0.9% + the 0.15% buffer
    assert r["plan"]["r"] == 2.0 and stop_widths(a)["plan"] == 2.5
    short = graded(-3.0, -2.5, sign=-1, plan={"entry": 100.0, "stop": 102.0, "target": 96.0})
    assert stop_widths(short)["plan"] == 2.0 and replay(short)["plan"]["r"] == -2.0
    assert replay({"edge": {"outcome": {"ACME": {"sign": 1, "close": 1.0}}}}) == {}  # no dip kept: no replay


def history(eng):
    """Twelve graded contract wins: winners that dip 1.5% first, losers that fall hard (a 1% stop
    shakes the winners out, 2% keeps them)."""
    for i in range(12):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (i + 3)
        win = i < 8
        a.edge = {"outcome": {"ACME": {"sign": 1, "h1": 0.5, "close": 2.0 if win else -6.0, "d1": 1.0}},
                  "exc": {"mae": -1.5 if win else -7.0, "mfe": 2.5}}  # fmt: skip
        eng.storage.add_alert(a)


def test_the_best_stop_per_kind(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    r = stop_rates(eng)["contract"]
    s = r["styles"]
    assert s["none"]["avg"] == round((8 * 2.0 - 4 * 6.0) / 12, 2)  # -0.67
    assert s["w1"]["avg"] == -1.0 and s["w1"]["stopped"] == 1.0 and s["w1"]["shaken"] == round(8 / 12, 2)
    assert s["w2"]["avg"] == round((8 * 2.0 - 4 * 2.0) / 12, 2) and s["w2"]["shaken"] == 0.0
    assert r["best"] == "w2" and r["width"] == 2.0
    line = stop_line(r)
    assert line.startswith("Stop contract win news here at 2.0% (+0.7% on average, 12); no stop -0.7%")
    assert line.endswith("a 1% stop shook out 67% of the calls it cut.")
    assert stop_price(100.0, "long", 2.0) == 98.0 and stop_price(50.0, "short", 3.0) == 51.5


async def test_the_stop_rides_the_alert_the_playbook_and_its_autopilot(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.refresh_base_rates()
    eng.stop_rates = {"contract": {"kind": "contract", "label": "Contract win", "n": 30, "best": "w3", "width": 3.0,
                                   "styles": {"w3": {"n": 30, "avg": 0.9, "median": 0.8, "hit": 0.6, "stopped": 0.2, "shaken": 0.3},
                                              "none": {"n": 30, "avg": 0.6, "median": 0.5, "hit": 0.6, "stopped": 0.0, "shaken": None}}}}  # fmt: skip
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    sr = eng.storage.get_alert(a.id)["edge"]["stop_rec"]
    assert sr["best"] == "w3" and sr["width"] == 3.0 and set(sr["styles"]) == {"w3", "none"}
    from news247.playbook import playbook

    pb = playbook(eng, "contract")
    assert any(r.startswith("Stop contract win news here at 3.0%") for r in pb["rules"])
    eng.storage.update_alert_edge(a.id, rec("alert", "d1"))
    assert await eng.autopilot_open(eng.storage.get_alert(a.id)) == 1
    t = eng.desk.get(DEV)["tracked"][-1]
    assert t["stop"] == round(t["entry"] * 0.97, 2) and t["target"] is None  # its kind's stop, a time exit


async def test_the_game_plan_names_the_stop(tmp_path, server):
    from .test_run37 import morning

    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    a = morning(eng)
    eng.storage.update_alert_edge(a.id, {**rec("open", "d1"), "stop_rec": {"best": "w3", "width": 3.0}})
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        assert await eng.send_open_plans(et(14, 9, 21)) >= 1
        assert "· in at the open, out the next day, stop 3.0%" in ph.read(server.requests[0]["body"])["body"]
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- what the record learned


def kind(label, entry, exit_, stop, hit=0.55, n=20, width=None):
    return {"label": label, "entry": entry, "entry_avg": 0.8, "exit": exit_, "exit_avg": 1.1, "stop": stop,
            "stop_avg": 0.6, "hit": hit, "n": n, **({"width": width} if width else {})}  # fmt: skip


def test_what_changed_in_a_week():
    old = {"kinds": {"contract": kind("Contract win", "alert", "close", "none", hit=0.52, n=30),
                     "fda": kind("FDA approval", "open", "d1", "w3", width=3.0)},
           "signals": {"leg2": {"label": "Second legs", "verdict": "fair", "hit": 0.55, "n": 20},
                       "turn": {"label": "The call turning to act", "verdict": "thin", "hit": None, "n": 3}}}  # fmt: skip
    new = {"kinds": {"contract": kind("Contract win", "alert", "d1", "w2", hit=0.61, n=34, width=2.0),
                     "fda": kind("FDA approval", "open", "d1", "w3", width=3.0),
                     "guidance": kind("Raised guidance", "open", "close", "heat", width=1.4, n=12, hit=0.58)},
           "signals": {"leg2": {"label": "Second legs", "verdict": "strong", "hit": 0.64, "n": 25},
                       "turn": {"label": "The call turning to act", "verdict": "fair", "hit": 0.5, "n": 12}}}  # fmt: skip
    items = diff(old, new)
    assert [x["what"] for x in items] == ["new", "exit", "stop", "odds", "signal", "signal"]
    assert items[0]["text"] == (
        "New on the record: raised guidance (12 graded, 58% its way by the close): "
        "in at the open, out at the close, a 1.4% stop."
    )
    assert items[1]["text"] == "Contract win: exit the next day now (+1.1% on average), was at the close."
    assert items[2]["text"] == "Contract win: a 2.0% stop now (+0.6% on average), was no stop."
    assert items[3]["text"] == "Contract win went their way 61% by the close, from 52% (34 graded)."
    assert items[4]["text"] == "Second legs: strong now (64%, 25), was fair."
    assert items[5]["text"] == "The call turning to act: fair now (50%, 12), new on the record."
    assert diff(new, new) == []
    p = push({"items": items}, 1000.0)
    assert p["title"] == "🧠 What the record learned this week (6)" and "+ 2 more in the app" in p["body"]
    assert push({"items": []}, 1000.0) is None


def test_snapshots_a_week_apart(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    eng.refresh_base_rates()  # saves today's snapshot
    now = time.time()
    snap = snapshot(eng)
    assert "contract" in snap["kinds"] and snap["kinds"]["contract"]["exit"] and snap["signals"]
    assert learned(eng, now)["since"] is None  # one day of snapshots: nothing to compare yet
    old = json.loads(json.dumps(snap))
    old["kinds"]["contract"]["exit"] = "h1" if snap["kinds"]["contract"]["exit"] != "h1" else "d5"
    del old["kinds"]["contract"]
    save_snapshot(eng, now - 8 * 86400)
    eng.storage.set_setting(f"learned:{previous(eng, now)[1]}", json.dumps(old))
    r = learned(eng, now)
    assert r["since"] and r["items"][0]["what"] == "new" and r["items"][0]["key"] == "contract"


async def test_the_friday_push_and_the_board(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    history(eng)
    eng.refresh_base_rates()
    now = time.time()
    save_snapshot(eng, now - 7 * 86400)
    eng.storage.set_setting(f"learned:{previous(eng, now)[1]}", json.dumps({"kinds": {}, "signals": {}}))
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        ph, _ = phones(eng, server)
        assert await eng.send_learned(now) >= 1
        msg = ph.read(next(r for r in server.requests if r["path"] == "/push/a")["body"])
        assert (
            msg["title"].startswith("🧠 What the record learned this week")
            and "New on the record" in msg["body"]
        )
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/learned"), headers=h) as r:
                body = await r.json()
                assert body["since"] and body["items"]
            async with s.get(srv.make_url("/api/stops-kind"), headers=h) as r:
                assert (await r.json())["styles"]["none"] == "No stop"
        eng.desk.apply(DEV, "prefs", {"learned": False})
        assert eng.desk.get(DEV)["prefs"]["learned"] is False
    finally:
        await srv.close()
        await eng.http.close()
