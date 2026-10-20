"""Run 57: which signals work here (every live signal's forward record ranked, with its switch and
advice) and a budget for signal pushes (an hourly cap per phone, the rest folded into a digest)."""

from __future__ import annotations

import time

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247 import pushbudget
from news247.signals import advice_items, scoreboard
from news247.web.server import WebServer

from .test_desk import DEV
from .test_personal import semis_engine
from .test_run3 import phones

# --------------------------------------------------------------------------- which signals work here


def records(eng):
    eng.leg_rates = {"*": {"key": "*", "label": "Second legs", "n": 20, "hit": 0.7, "median": 1.4}}
    eng.turn_rates = {"act": {"verb": "act", "n": 15, "hit": 0.4, "median": -0.6}}
    eng.catchup_rates = {
        "*|*": {"key": "*|*", "n": 4, "after": {"n": 4, "hit": 0.5, "median": 0.1}, "captured": None}
    }


def test_the_scoreboard(tmp_path):
    eng = semis_engine(tmp_path)
    records(eng)
    eng.desk.apply(DEV, "prefs", {"leg2": False})
    rows = {r["key"]: r for r in scoreboard(eng, DEV)["rows"]}
    assert (rows["leg2"]["verdict"], rows["leg2"]["on"]) == ("strong", False)
    assert (rows["turn"]["verdict"], rows["turn"]["on"], rows["turn"]["pref"]) == ("weak", True, "callnow")
    assert rows["catchup"]["verdict"] == "thin" and rows["volconf"]["n"] == 0
    order = [r["key"] for r in scoreboard(eng, DEV)["rows"]]
    assert order[0] == "leg2" and order.index("turn") < order.index(
        "catchup"
    )  # strong, then weak, then too few
    items = {i["key"]: i for i in advice_items(eng, DEV)}
    assert items["sig-on:leg2"]["body"] == {"leg2": True}
    assert items["sig-off:callnow"]["title"] == "Turn off the call turning to act pushes"
    assert items["sig-off:callnow"]["why"] == "They're weak here: 40% from the turn to the close (15 graded)."


def test_the_advice_includes_the_signals(tmp_path):
    from news247.advice import advise

    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
    records(eng)
    keys = [i["key"] for i in advise(eng, DEV)["items"]]
    assert "sig-off:callnow" in keys


# --------------------------------------------------------------------------- the budget


def test_signal_pushes_are_budgeted(tmp_path):
    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "prefs", {"signal_cap": 4})
    now = 1000.0
    assert pushbudget.admit(
        eng, DEV, {"id": "pa-1", "title": "price alert"}, now
    )  # not a signal: never folded
    got = [
        pushbudget.admit(eng, DEV, {"id": f"l2-{i}", "title": f"🚀 ACME second leg {i}: x"}, now + i)
        for i in range(6)
    ]
    assert got == [True, True, True, True, False, False]
    p = eng.desk.profiles[DEV]
    assert [f["title"] for f in p["signal_folded"]] == ["🚀 ACME second leg 4: x", "🚀 ACME second leg 5: x"]
    assert pushbudget.admit(eng, DEV, {"id": "cu-9", "title": "📰 CNBC has BETA"}, now + 3601)  # an hour on
    eng.desk.apply(DEV, "prefs", {"signal_cap": 0})
    assert all(pushbudget.admit(eng, DEV, {"id": f"vc-{i}"}, now + 4000 + i) for i in range(20))  # no limit
    with pytest.raises(ValueError):
        eng.desk.apply(DEV, "prefs", {"signal_cap": 5})


async def test_folded_signals_come_in_a_digest(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "prefs", {"signal_cap": 4})
        sent = 0
        for i, sym in enumerate(["ACME", "BETA", "DLTA", "GAMA", "ACME", "BETA"]):
            sent += await eng._to_device(
                DEV,
                {"id": f"l2-{i}", "title": f"🚀 {sym} second leg: a new high", "body": "x", "tickers": [sym]},
            )
        assert sent == 4 and len(server.requests) == 4
        assert await pushbudget.send_digest(eng, time.time()) == 1
        msg = ph.read(server.requests[-1]["body"])
        assert msg["title"] == "🗂 2 more signals while your budget was full"
        assert msg["body"] == "🚀 ACME second leg · 🚀 BETA second leg"
        assert await pushbudget.send_digest(eng, time.time() + 60) == 0  # nothing folded since
    finally:
        await eng.http.close()


async def test_run57_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    records(eng)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s, s.get(srv.make_url("/api/signals"), headers=h) as r:
            assert (await r.json())["rows"][0]["key"] == "leg2"
    finally:
        await srv.close()
        await eng.http.close()
