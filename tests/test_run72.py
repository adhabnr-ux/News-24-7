"""Run 72: scaling out, graded (half at each of two exits, as pairs in the same machinery, and the
playbook autopilot taking a winning split as two half-size paper calls) and show the work (the alerts
behind any pair's number)."""

from __future__ import annotations

import time

import aiohttp
from aiohttp.test_utils import TestServer

from news247.autopilot import exit_time, opened_today
from news247.combos import NAMES, PAIRS, combo_rates, combos, work
from news247.today import state
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run60 import et, rec
from .test_run64 import alert, pair

# --------------------------------------------------------------------------- scaling out


def test_the_split_pairs():
    assert "h1+close" in PAIRS["alert"] and "close+d1" in PAIRS["alert"] and "h1+d1" in PAIRS["alert"]
    assert (
        "close+d1" in PAIRS["leg2"] and "h1+close" not in PAIRS["leg2"]
    )  # a second leg can't be sold an hour in
    assert NAMES["alert|close+d1"] == "At the alert, out half at the close, half the next day"
    a = {
        "edge": {
            "refs": {"ACME": 100.0},
            "outcome": {"ACME": {"sign": 1, "h1": 1.0, "close": 3.0, "d1": 5.0}},
        }
    }
    c = combos(a)
    assert c["alert|h1+close"] == 2.0 and c["alert|close+d1"] == 4.0 and c["alert|h1+d1"] == 3.0


def volatile(eng):
    """Contract wins whose next day swings wide (+6% or -1%): half an hour in, half the next day keeps most of it, steadier."""
    for i in range(12):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (20 - i)
        a.edge = {"outcome": {"ACME": {"sign": 1, "h1": 2.0, "close": 0.5, "d1": 6.0 if i % 2 else -1.0}}}
        eng.storage.add_alert(a)


def test_a_smoother_split(tmp_path):
    eng = semis_engine(tmp_path)
    volatile(eng)
    r = combo_rates(eng)["contract"]
    assert r["best"] == "alert|d1" and r["styles"]["alert|d1"]["avg"] == 2.5
    sm = r["smooth"]
    assert sm["pair"] == "alert|h1+d1" and sm["avg"] == 2.25 and sm["sd"] == round(sm["best_sd"] / 2, 2)
    from news247.combos import combo_line

    assert "; smoother: half an hour in, half the next day, +2.2% with 50% less swing" in combo_line(r)
    from news247.autopilot import playbook_out

    cr = {"best": "alert|d1", "in": "alert", "out": "d1", "smooth": sm}
    assert playbook_out({"edge": {"combo_rec": cr}}) == "h1+d1"  # the playbook takes the steadier way
    from news247.forward import promise

    assert promise({"combo_rec": cr})["promise"] == 2.25 and promise({"combo_rec": cr})["out"] == "h1+d1"


async def test_the_autopilot_scales_out_in_two_halves(tmp_path):
    from .test_run16 import contract_history

    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.refresh_base_rates()
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    e = rec("alert", "h1+d1")
    e["combo_rec"] = {"best": "alert|h1+d1", "in": "alert", "out": "h1+d1", "kind": "contract", "label": "Contract win",
                      "styles": {"alert|h1+d1": {"avg": 3.0}}}  # fmt: skip
    eng.storage.update_alert_edge(a.id, e)
    assert await eng.autopilot_open(eng.storage.get_alert(a.id)) == 1
    p = eng.desk.get(DEV)
    halves = [t for t in p["tracked"] if t.get("alert_id") == a.id]
    assert [t["half"] for t in halves] == [1, 2]
    assert abs(halves[0]["exit_at"] - exit_time("h1", halves[0]["ts"])) < 2  # each half its own exit
    assert abs(halves[1]["exit_at"] - exit_time("d1", halves[1]["ts"], eng.calendar)) < 2
    assert (
        opened_today(p, halves[0]["auto"], time.time()) == 1
    )  # one alert, not two calls, against the daily limit


def test_the_board_follows_both_halves():
    t0 = et(14, 10)
    s = state(
        alert(t0, **pair("alert", "h1+close"), outcome={"ACME": {"sign": 1, "h1": 2.0}}), 1.0, t0 + 4000
    )
    assert (
        s["status"] == "in" and s["why"] == "half out" and s["pnl"] == 1.5
    )  # +2% on the first half, +1% live on the second
    done = alert(t0, **pair("alert", "h1+close"), outcome={"ACME": {"sign": 1, "h1": 2.0, "close": 3.0}})
    s = state(done, 3.0, t0 + 8 * 3600)
    assert s["status"] == "out" and s["pnl"] == 2.5


# --------------------------------------------------------------------------- show the work


def test_the_alerts_behind_a_pair(tmp_path):
    eng = semis_engine(tmp_path)
    volatile(eng)
    w = work(eng, "contract", "alert|h1+d1")
    assert (
        w["n"] == 12
        and w["name"] == NAMES["alert|h1+d1"]
        and w["label"] == "Contract win"
        and w["avg"] == 2.25
    )
    assert w["rows"][0]["created"] > w["rows"][-1]["created"]  # newest first
    assert w["rows"][0]["x"] == round((2.0 + 6.0) / 2, 2) and w["rows"][0]["plain"] == 0.5
    assert work(eng, "fda", "alert|close")["n"] == 0


async def test_the_work_endpoint(tmp_path):
    eng = semis_engine(tmp_path)
    volatile(eng)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                srv.make_url("/api/combos/work"), headers=h, params={"kind": "contract", "pair": "alert|d1"}
            ) as r:
                body = await r.json()
                assert body["n"] == 12 and len(body["rows"]) == 12
            async with s.get(
                srv.make_url("/api/combos/work"), headers=h, params={"kind": "contract", "pair": "nope"}
            ) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()
