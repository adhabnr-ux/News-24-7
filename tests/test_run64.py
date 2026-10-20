"""Run 64: the playbook today (every live alert's way in and out, where it stands, on paper, against
the plain way) and you against the playbook (your paper calls beside the record's way on the same
alerts)."""

from __future__ import annotations

import time

import aiohttp
from aiohttp.test_utils import TestServer

from news247.desk import session_edge
from news247.today import board, state
from news247.web.server import WebServer
from news247.youvs import compare, playbook_result

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run60 import et


def pair(pin, pout, **edge):
    return {
        "combo_rec": {"best": f"{pin}|{pout}", "in": pin, "out": pout, "label": "Contract win", "held": True},
        **edge,
    }


def alert(created, **edge):
    return {"id": "a1", "created": created, "tickers": ["ACME"], "title": "Acme wins contract", "kind": "news",
            "analysis": {"direction": "up"}, "edge": {"refs": {"ACME": 100.0}, **edge}}  # fmt: skip


# --------------------------------------------------------------------------- the playbook today


def test_waiting_in_and_out():
    t0 = et(14, 10)  # a Wednesday, 10:00 ET
    # in at the alert, out at the close: in now, marked to the live price
    s = state(alert(t0, **pair("alert", "close")), 2.0, t0 + 600)
    assert s["status"] == "in" and s["pnl"] == 2.0 and s["entry"] == 100.0 and s["plain"] == 2.0
    # waiting for a second leg, then in on it, then out at the close
    s = state(alert(t0, **pair("leg2", "close")), 1.0, t0 + 600)
    assert s["status"] == "waiting" and s["why"] == "for a second leg"
    s = state(alert(t0, **pair("leg2", "close"), leg2={"px": 102.0}), 3.0, t0 + 7200)
    assert s["status"] == "in" and s["entry"] == 102.0 and s["pnl"] == round((1.03 / 1.02 - 1) * 100, 2)
    done = alert(t0, **pair("leg2", "close"), leg2={"px": 102.0}, outcome={"ACME": {"sign": 1, "close": 4.0}})
    s = state(done, 4.5, t0 + 8 * 3600)
    assert s["status"] == "out" and s["pnl"] == round((1.04 / 1.02 - 1) * 100, 2) and s["plain"] == 4.0
    # no second leg by the close: skipped
    s = state(alert(t0, **pair("leg2", "close")), 1.0, t0 + 8 * 3600)
    assert s["status"] == "skipped" and s["why"] == "no second leg by the close"


def test_patient_open_volume_and_the_bracket():
    t0 = et(14, 10)
    pt = {"plan": {"entry": 100.0, "stop": 98.0, "target": 104.0, "patient": {"dip": 1.0, "limit": 99.0}}}
    s = state(alert(t0, **pair("patient", "d1"), **pt), 0.5, t0 + 600)
    assert s["status"] == "waiting" and "for its limit, 1.0% against ($99.00)" in s["why"]
    s = state(alert(t0, **pair("patient", "d1"), **pt, exc={"mae": -1.2, "mfe": 2.0}), 1.0, t0 + 3600)
    assert s["status"] == "in" and s["entry"] == 99.0 and s["pnl"] == round((1.01 / 0.99 - 1) * 100, 2)
    pre = et(14, 8)  # before the bell
    assert state(alert(pre, **pair("open", "close")), 0.0, pre + 600)["why"] == "for the open"
    s = state(
        alert(pre, **pair("open", "close"), outcome={"ACME": {"sign": 1, "open": 3.0}}), 2.0, et(14, 11)
    )
    assert s["status"] == "in" and s["entry"] == 103.0 and s["pnl"] < 0  # it gave back the gap
    s = state(alert(t0, **pair("vol15", "close"), rvol={"r15": 2.0}), 1.0, t0 + 1200)
    assert s["status"] == "skipped" and s["why"].startswith("volume didn't confirm (2.0x")
    s = state(alert(t0, **pair("alert", "bracket"), **pt, exc={"mae": -2.5, "mfe": 1.0}), -1.0, t0 + 3600)
    assert s["status"] == "out" and s["pnl"] == -2.0 and s["why"] == "at the plan's stop"
    assert state(alert(t0), 1.0, t0) is None  # no playbook for its kind: not on the board


def test_the_board(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    a = news("Acme wins $2B Pentagon contract")
    a.created = now - 600
    a.edge = {"refs": {"ACME": 100.0}, **pair("alert", "close")}
    eng.storage.add_alert(a)
    old = news("Acme wins Navy contract")
    old.created = session_edge("close", now - 86400 * 6, eng.calendar) - 3600  # held for five sessions
    old.edge = {
        "refs": {"ACME": 100.0},
        **pair("alert", "d5"),
        "outcome": {"ACME": {"sign": 1, "close": 1.0}},
    }
    eng.storage.add_alert(old)
    stale = news("Acme wins Army contract")
    stale.created = now - 86400 * 6
    stale.edge = {
        "refs": {"ACME": 100.0},
        **pair("alert", "close"),
        "outcome": {"ACME": {"sign": 1, "close": 1.0}},
    }
    eng.storage.add_alert(stale)
    eng.live_quotes.update("ACME", 103.0, 99.0, now - 5, "tape")
    b = board(eng, now)
    ids = [r["id"] for r in b["rows"]]
    assert a.id in ids and stale.id not in ids  # done and old: off the board
    row = b["rows"][ids.index(a.id)]
    assert row["status"] == "in" and row["pnl"] == 3.0 and row["pair"] == "At the alert, out at the close"
    held = b["rows"][ids.index(old.id)]  # still held into its fifth session: on the board
    assert held["status"] == "in" and held["pnl"] == 3.0 and held["plain"] == 1.0
    assert b["tally"]["in"] == 2 and b["tally"]["vs_plain"] == 1.0  # (0 + 2) / 2


# --------------------------------------------------------------------------- you vs the playbook


def graded(eng, i, d1, close=1.0, **edge):
    a = news(f"Acme wins Pentagon contract {i}")
    a.created = time.time() - 86400 * (i + 2)
    a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1, "close": close, "d1": d1}}, **edge}
    eng.storage.add_alert(a)
    return a


def test_the_playbooks_result_on_an_alert(tmp_path):
    eng = semis_engine(tmp_path)
    a = graded(eng, 0, 3.0, **pair("alert", "d1"))
    assert playbook_result(eng.storage.get_alert(a.id)) == (3.0, "alert|d1")
    b = graded(eng, 1, 3.0, **pair("leg2", "d1"))  # no second leg came: a pass
    assert playbook_result(eng.storage.get_alert(b.id)) == (0.0, "leg2|d1")
    c = graded(eng, 2, 3.0)
    assert playbook_result(eng.storage.get_alert(c.id)) == (1.0, "alert|close")  # no playbook: the plain way


def test_you_against_the_playbook(tmp_path):
    eng = semis_engine(tmp_path)
    p = eng.desk.get(DEV)
    for i in range(10):  # you sell at noon for +0.5%; the playbook held into the next day for +3%
        a = graded(eng, i, 3.0, **pair("alert", "d1"))
        p.setdefault("closed", []).append({"id": f"c{i}", "symbol": "ACME", "direction": "long", "alert_id": a.id,
                                           "pnl_pct": 0.5, "closed": a.created + 7200})  # fmt: skip
    p["closed"].append(
        {"id": "auto", "symbol": "ACME", "direction": "long", "alert_id": a.id, "pnl_pct": 9.0, "auto": "r1"}
    )
    r = compare(eng, DEV)
    assert (
        r["n"] == 10
        and r["you"] == 0.5
        and r["playbook"] == 3.0
        and r["plain"] == 1.0
        and r["verdict"] == "playbook"
    )
    assert (
        r["line"]
        == "Following the playbook on your 10 calls would have made +2.50% a call more (+3.00% against your +0.50%)."
    )
    assert (
        r["kinds"][0]["label"] == "Contract win"
        and r["widest"][0]["pair"] == "At the alert, out the next day"
    )
    short = graded(eng, 30, 3.0, **pair("alert", "d1"))
    p["closed"].append(
        {"id": "s", "symbol": "ACME", "direction": "short", "alert_id": short.id, "pnl_pct": -1.0}
    )
    assert compare(eng, DEV)["widest"][0]["playbook"] in (3.0, -3.0)


async def test_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/playbook-today"), headers=h) as r:
                assert (await r.json())["rows"] == []
            async with s.get(srv.make_url("/api/you-vs-playbook"), headers=h) as r:
                assert (await r.json())["n"] == 0
    finally:
        await srv.close()
        await eng.http.close()
