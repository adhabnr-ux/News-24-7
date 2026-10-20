"""Run 71: the playbook coaches your open calls (its exit verdicts on the calls you opened, in Needs
you and as pushes) and the promise monitor (each kind's pair, promised against what it made on the
alerts after it, and a drifting kind sat out)."""

from __future__ import annotations

import time

from news247.actions import action_center
from news247.autopilot import playbook_pass
from news247.callcoach import KEY, send, verdicts
from news247.desk import session_edge
from news247.promises import LIVE_N, board, line, live_record

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones
from .test_run64 import pair

# --------------------------------------------------------------------------- the playbook coaches your calls


def my_call(eng, created, **edge):
    a = news("Acme wins $2B Pentagon contract")
    a.created = created
    a.edge = {"refs": {"ACME": 100.0}, **edge}
    eng.storage.add_alert(a)
    eng.desk.apply(
        DEV, "track_add", {"symbol": "ACME", "direction": "long", "alert_id": a.id, "entry": 100.0}
    )
    return a, eng.desk.get(DEV)["tracked"][-1]


def test_its_way_out_came(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    a, t = my_call(eng, now - 4000, **pair("alert", "h1"))
    eng.live_quotes.update("ACME", 101.0, 99.0, now - 5, "tape")
    v = verdicts(eng, DEV, now)
    assert [x["kind"] for x in v] == ["exit"] and v[0][
        "title"
    ] == "ACME: the playbook's way out is an hour in"
    item = next(i for i in action_center(eng, DEV, now)["items"] if i["key"].startswith("coach:"))
    assert (
        item["do"]["action"] == "track_close"
        and item["do"]["body"]["id"] == t["id"]
        and item["open"] == f"#a={a.id}"
    )


def test_a_denied_call_and_an_autopilot_call(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    a, _ = my_call(
        eng, now - 600, **pair("alert", "close"), denied={"by": "x", "title": "Acme denies the contract"}
    )
    v = verdicts(eng, DEV, now)
    assert v[0]["kind"] == "denied" and v[0]["urgency"] == 82 and "Acme denies the contract" in v[0]["detail"]
    eng.desk.get(DEV)["tracked"][-1]["auto"] = "r1"  # the autopilot's own calls run themselves
    assert verdicts(eng, DEV, now) == []


def test_a_stalled_call(tmp_path):
    from .test_run67 import stalls

    eng = semis_engine(tmp_path)
    stalls(eng)
    eng.refresh_base_rates()
    now = time.time()
    my_call(
        eng,
        now - 1900,
        **pair("alert", "close"),
        catalyst={"key": "contract", "label": "Contract win", "band": "large"},
    )
    eng.live_quotes.update("ACME", 100.1, 99.0, now - 5, "tape")
    v = verdicts(eng, DEV, now)
    assert v[0]["kind"] == "stall" and v[0]["detail"].endswith("the time stop says out.")


async def test_the_coach_push_once(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        now = time.time()
        my_call(
            eng, now - 600, **pair("alert", "close"), denied={"by": "x", "title": "Acme denies the contract"}
        )
        assert await send(eng, now) == 1
        msg = ph.read(next(r for r in server.requests if r["path"] == "/push/a")["body"])
        assert msg["title"] == "🧭 ACME: its news was denied" and msg["severity"] == "HIGH"
        assert await send(eng, now + 60) == 0 and eng.storage.get_setting(KEY)
        eng.desk.apply(DEV, "prefs", {"coach_calls": False})
        assert eng.desk.get(DEV)["prefs"]["coach_calls"] is False
    finally:
        await eng.http.close()


def test_the_close_verdict_before_an_overnight_hold(tmp_path):
    from .test_run66 import history

    eng = semis_engine(tmp_path)
    history(eng)
    eng.refresh_base_rates()
    close = session_edge("close", time.time(), eng.calendar)
    now = close - 300
    my_call(eng, close - 5 * 3600, **pair("alert", "d1"), exc={"mfe": 4.0, "mae": -0.2},
            catalyst={"key": "contract", "label": "Contract win", "band": "large"})  # fmt: skip
    eng.live_quotes.update("ACME", 100.4, 99.0, now - 5, "tape")
    v = verdicts(eng, DEV, now)
    assert v[0]["kind"] == "giveback" and v[0]["title"] == "ACME: out at the close instead of overnight?"


# --------------------------------------------------------------------------- the promise monitor


def stamped(eng, n, promise, made, lo=None):
    for i in range(n):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (n + 2 - i)
        cr = pair("alert", "d1")["combo_rec"]
        cr.update(
            kind="contract", styles={"alert|d1": {"avg": promise, **({"lo": lo} if lo is not None else {})}}
        )
        a.edge = {"combo_rec": cr, "outcome": {"ACME": {"sign": 1, "close": 0.5, "d1": made}}}
        eng.storage.add_alert(a)


def test_a_kind_keeping_its_word(tmp_path):
    eng = semis_engine(tmp_path)
    stamped(eng, LIVE_N, 1.2, 1.0, lo=0.4)
    r = live_record(eng)["contract"]
    assert r["n"] == LIVE_N and r["live"] == 1.0 and r["promised"] == 1.2 and r["verdict"] == "keeping"
    assert line(r).endswith("keeping its word.")


def test_a_drifting_kind_is_sat_out(tmp_path):
    eng = semis_engine(tmp_path)
    stamped(eng, LIVE_N + 5, 1.2, -0.8, lo=0.4)
    r = live_record(eng)["contract"]
    assert r["n"] == LIVE_N and r["verdict"] == "drifting"
    assert line(r) == (f"Its last {LIVE_N} live alerts made -0.8% on its pair against +1.2% promised (the range's bottom +0.4%): "
                       "drifting, so the playbook sits it out until it recovers.")  # fmt: skip
    assert board({"contract": r})["rows"][0]["verdict"] == "drifting"
    thin = live_record(semis_engine(tmp_path / "b"))
    assert thin == {}


async def test_a_drifting_kind_rides_the_alert(tmp_path):
    from .test_run63 import history

    eng = semis_engine(tmp_path)
    history(eng)  # graded contract wins: their pair is fitted...
    eng.refresh_base_rates()
    eng.promise_rates = {"contract": {"kind": "contract", "label": "Contract win", "n": LIVE_N, "live": -0.8, "promised": 1.2,
                                      "lo": 0.4, "verdict": "drifting"}}  # fmt: skip
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    got = eng.storage.get_alert(a.id)
    assert got["edge"]["combo_rec"]["drift"]["live"] == -0.8
    assert playbook_pass(got).startswith(f"its kind is drifting: the last {LIVE_N} live alerts made -0.8%")
