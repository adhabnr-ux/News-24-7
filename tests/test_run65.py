"""Run 65: the playbook on the lock screen (a push when the playbook's own way in comes and when it's
out, to the phones on the stock, once each, budgeted) and the desk acting on the playbook's record
(advice to follow it when it beats your calls, to pause an autopilot that costs against the plain
way)."""

from __future__ import annotations

import time

from news247.advice import advise
from news247.pbpush import KEY, send_moves
from news247.pushbudget import is_signal

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones
from .test_run62 import closed_call
from .test_run64 import graded, pair


def live(eng, now, pin, pout, **edge):
    a = news("Acme wins $2B Pentagon contract")
    a.created = now - 7200
    a.edge = {"refs": {"ACME": 100.0}, **pair(pin, pout), **edge}
    a.edge["combo_rec"]["styles"] = {f"{pin}|{pout}": {"avg": 1.4, "n": 12}}
    eng.storage.add_alert(a)
    return a


async def test_the_playbooks_moves_reach_the_lock_screen(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        now = time.time()
        eng.storage.set_setting(KEY, "{}")  # not the first run after an upgrade
        eng.live_quotes.update("ACME", 103.0, 99.0, now - 5, "tape")
        a = live(eng, now, "leg2", "d1")
        assert await send_moves(eng, now) == 0  # waiting for its second leg: nothing yet
        eng.storage.update_alert_edge(a.id, {"leg2": {"px": 102.0}})
        assert await send_moves(eng, now + 60) == 1
        msg = ph.read(server.requests[-1]["body"])
        assert msg["title"] == "🧭 ACME: the playbook's way in, on the second leg at $102.00"
        assert msg["body"].startswith("Out the next day · its pair made +1.4% on average here (12)")
        assert msg["open"] == f"/app/#a={a.id}"
        assert await send_moves(eng, now + 120) == 0  # once
        eng.storage.update_alert_edge(a.id, {"outcome": {"ACME": {"sign": 1, "close": 2.0, "d1": 4.0}}})
        assert await send_moves(eng, now + 180) == 1
        out = ph.read(server.requests[-1]["body"])
        assert out["title"].startswith(
            "🏁 ACME: out the next day, +2.0% on the playbook (the plain way +2.0%)"
        )
        plain = live(eng, now, "alert", "close", outcome={"ACME": {"sign": 1, "close": 1.0}})
        assert await send_moves(eng, now + 200) == 0  # the plain pair: in with the alert, out with the bell
        assert f"{plain.id}:out" not in eng.storage.get_setting(KEY)
        eng.desk.apply(DEV, "prefs", {"pb_push": False})
        b = live(eng, now, "leg2", "close", leg2={"px": 101.0})
        assert await send_moves(eng, now + 240) == 0  # switched off
        assert is_signal({"id": f"pb-in-{b.id}"}) and is_signal(
            {"id": f"pb-out-{b.id}"}
        )  # on the signal budget
    finally:
        await eng.http.close()


async def test_the_first_run_pushes_nothing_old(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        phones(eng, server)
        now = time.time()
        live(eng, now, "leg2", "close", leg2={"px": 101.0})
        assert await send_moves(eng, now) == 0 and eng.storage.get_setting(KEY) != "{}"
        assert await send_moves(eng, now + 60) == 0  # already seen
    finally:
        await eng.http.close()


def test_advice_to_follow_the_playbook(tmp_path):
    eng = semis_engine(tmp_path)
    p = eng.desk.get(DEV)
    for i in range(10):
        a = graded(eng, i, 3.0, **pair("alert", "d1"))
        p.setdefault("closed", []).append({"id": f"c{i}", "symbol": "ACME", "direction": "long", "alert_id": a.id,
                                           "pnl_pct": 0.5, "closed": a.created + 7200})  # fmt: skip
    item = next(i for i in advise(eng, DEV)["items"] if i["key"] == "pb_follow")
    assert item["action"] == "autopilot_add" and item["body"] == {"follow": "playbook", "min_sev": "MEDIUM"}
    assert "the playbook made +3.00% a call on the same alerts against your +0.50%" in item["why"]
    eng.desk.apply(DEV, item["action"], item["body"])
    assert not any(i["key"] == "pb_follow" for i in advise(eng, DEV)["items"])  # done: it follows it now


def test_advice_to_pause_an_autopilot_that_costs(tmp_path):
    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    rid = eng.desk.get(DEV)["autopilots"][0]["id"]
    for i in range(20):
        closed_call(eng, i, -0.5, 1.0, rid=rid)  # live -0.5% a call where the plain way made +1.0%
    item = next(i for i in advise(eng, DEV)["items"] if i["key"] == f"ap_pause:{rid}")
    assert (
        item["action"] == "autopilot_toggle"
        and item["body"] == {"id": rid}
        and "its choices cost -1.50%" in item["why"]
    )
    eng.desk.apply(DEV, item["action"], item["body"])
    assert eng.desk.get(DEV)["autopilots"][0]["on"] is False
    assert not any(i["key"].startswith("ap_pause") for i in advise(eng, DEV)["items"])
