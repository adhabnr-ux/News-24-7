"""Run 59: how to exit each kind of news (five exits graded side by side on the same alerts) and your
reaction time, priced (push to paper call, what the wait cost), with a Track button on the lock screen."""

from __future__ import annotations

import time

import aiohttp
from aiohttp.test_utils import TestServer

from news247.exits_kind import exit_line, exit_rates, exits
from news247.reaction import calls, reaction, speed
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine

# --------------------------------------------------------------------------- how to exit


def test_one_alert_exited_five_ways():
    a = {"edge": {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1, "h1": 1.0, "close": 2.0, "d1": 3.5, "d5": -1.0}},
                  "plan": {"stop": 98.0, "target": 103.0}, "exc": {"mae": -1.0, "mfe": 3.2}}}  # fmt: skip
    assert exits(a) == {
        "h1": 1.0,
        "close": 2.0,
        "d1": 3.5,
        "d5": -1.0,
        "bracket": 3.0,
    }  # the target was reached
    stopped = {"edge": {**a["edge"], "exc": {"mae": -2.5, "mfe": 3.2}}}
    assert exits(stopped)["bracket"] == -2.0  # both reached: the stop is assumed first
    neither = {"edge": {**a["edge"], "exc": {"mae": -0.5, "mfe": 1.0}}}
    assert exits(neither)["bracket"] == 2.0  # out at the close
    short = {"edge": {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": -1, "close": -2.0}},
                      "plan": {"stop": 102.0, "target": 97.0}, "exc": {"mae": -0.5, "mfe": 3.5}}}  # fmt: skip
    assert exits(short) == {"close": 2.0, "bracket": 3.0}


def test_the_best_way_out_per_kind(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(10):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (i + 8)
        a.edge = {
            "outcome": {
                "ACME": {"sign": 1, "h1": 0.5, "close": 1.0 if i < 6 else -1.0, "d1": 2.0 if i < 7 else -1.0}
            }
        }
        eng.storage.add_alert(a)
    r = exit_rates(eng)["contract"]
    assert r["best"] == "d1" and r["styles"]["close"]["avg"] == 0.2 and r["styles"]["d1"]["avg"] == 1.1
    assert (
        exit_line(r)
        == "Exit contract win news here: the next day (+1.1% on average, 70% its way, 10); at the close +0.2%."
    )


# --------------------------------------------------------------------------- your reaction time


def tracked(eng, delay, entry, via=""):
    a = news(f"Acme wins Pentagon contract {delay} {entry}")
    a.created = time.time() - 86400
    a.edge = {"refs": {"ACME": 100.0}}
    eng.storage.add_alert(a)
    eng.desk.apply(DEV, "track_add", {"symbol": "ACME", "direction": "long", "alert_id": a.id, "entry": entry,
                                      **({"via": via} if via else {})})  # fmt: skip
    eng.desk.profiles[DEV]["tracked"][-1]["ts"] = a.created + delay
    return a


def test_reaction_time_and_its_cost(tmp_path):
    eng = semis_engine(tmp_path)
    eng.clock_rates = {"contract": {"half": "m15"}}
    for delay, entry in ((120, 100.2), (200, 100.4), (600, 101.0), (1500, 101.5), (2400, 102.0)):
        tracked(eng, delay, entry)
    tracked(eng, 90, 100.1, via="lockscreen")
    cs = calls(eng, DEV)
    assert (
        len(cs) == 6 and cs[0]["cost"] == round((100 / 100.2 - 1) * 100, 2) and cs[-1]["via"] == "lockscreen"
    )
    r = reaction(eng, DEV)
    assert r["n"] == 6 and r["delay"] == 400  # the median of 90, 120, 200, 600, 1500, 2400 seconds
    assert [s["speed"] for s in r["speeds"]] == ["fast", "soon", "late"]
    assert r["speeds"][0]["n"] == 3 and r["speeds"][-1]["cost"] < r["speeds"][0]["cost"]  # slower costs more
    k = r["kinds"][0]
    assert k["kind"] == "contract" and k["half_label"] == "15 minutes"
    assert r["lockscreen"]["n"] == 1 and r["lockscreen"]["cost"] > r["lockscreen"]["app_cost"]
    assert [speed(x) for x in (10, 300, 1000, 9000)] == ["fast", "soon", "late", "slow"]


def test_autopilot_calls_dont_count(tmp_path):
    eng = semis_engine(tmp_path)
    tracked(eng, 60, 100.0)
    eng.desk.profiles[DEV]["tracked"][-1]["auto"] = "r1"
    assert calls(eng, DEV) == []


# --------------------------------------------------------------------------- Track from the lock screen


def test_alert_pushes_carry_a_track_button():
    from news247.notify.channels import WebPushNotifier

    a = news("Acme wins $2B Pentagon contract")
    a.edge = {
        "plan": {"dir": "up", "symbol": "ACME", "entry": 100.0, "stop": 98.0, "target": 104.0, "rr": 2.0}
    }
    assert WebPushNotifier.payload(None, a)["track"] == a.id
    b = news("Acme raises guidance")
    assert WebPushNotifier.payload(None, b)["track"] == ""  # no plan: nothing to track with


async def test_track_from_the_lock_screen(tmp_path):
    from .test_run16 import contract_history

    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.refresh_base_rates()
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(srv.make_url("/api/track-alert"), headers=h, json={"alert_id": a.id}) as r:
                body = await r.json()
                assert body["ok"] and body["symbol"] == "ACME"
            async with s.post(srv.make_url("/api/track-alert"), headers=h, json={"alert_id": "nope"}) as r:
                assert r.status == 404
            async with s.get(srv.make_url("/api/reaction"), headers=h) as r:
                assert (await r.json())["n"] == 1
            async with s.get(srv.make_url("/api/exits-kind"), headers=h) as r:
                assert (await r.json())["styles"]["bracket"] == "The plan's bracket"
        t = eng.desk.get(DEV)["tracked"][-1]
        assert t["via"] == "lockscreen" and t["stop"] and t["target"] and t["alert_id"] == a.id
    finally:
        await srv.close()
        await eng.http.close()
