"""Run 66: the playbook passes (when no way in and out made money on its kind here) and holds that
gave back their run (the overnight record by how much of the day's run a stock kept, and the 15:50
exit for playbook calls whose kind of day lost overnight)."""

from __future__ import annotations

import time

from news247.autopilot import matches, playbook_in
from news247.desk import session_edge
from news247.giveback import bucket, giveback_rates, verdict
from news247.today import state
from news247.youvs import playbook_result

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run60 import et
from .test_run64 import alert, pair

# --------------------------------------------------------------------------- the playbook passes


def passing(pin="alert", pout="close", avg=-0.4):
    e = pair(pin, pout)
    e["combo_rec"]["styles"] = {f"{pin}|{pout}": {"avg": avg, "n": 20}}
    e["combo_rec"]["pass"] = avg <= 0
    return e


def test_the_playbook_passes_on_a_kind_that_loses():
    a = {**news("Acme wins contract").to_dict(), "edge": passing()}
    assert playbook_in(a) is None
    from news247.autopilot import clean_rule

    rule = clean_rule({"follow": "playbook", "min_sev": "MEDIUM"}, 0)
    assert not matches(rule, a)  # the autopilot sits it out
    t0 = et(14, 10)
    s = state(alert(t0, **passing()), 1.0, t0 + 600)
    assert (
        s["status"] == "pass" and s["why"] == "no way in and out made money on its kind here (the best -0.4%)"
    )
    keep = {**news("Acme wins contract").to_dict(), "edge": passing(avg=0.6)}
    assert playbook_in(keep) == "alert"


async def test_a_pass_is_stamped_at_publish(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(12):  # contract wins that lose every way here
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (20 - i)
        a.edge = {
            "refs": {"ACME": 100.0},
            "outcome": {"ACME": {"sign": 1, "h1": -0.5, "close": -1.0, "d1": -2.0, "d5": -3.0}},
        }
        eng.storage.add_alert(a)
    eng.refresh_base_rates()
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    cr = eng.storage.get_alert(a.id)["edge"]["combo_rec"]
    assert cr["pass"] is True and cr["styles"][cr["best"]]["avg"] <= 0


def test_a_pass_counts_as_zero_against_your_calls():
    a = {"edge": {**passing(), "refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1, "close": 2.0}}}}
    assert playbook_result(a) == (0.0, "pass")


# --------------------------------------------------------------------------- holds that gave back their run


def test_how_much_of_its_run_it_kept():
    assert bucket(3.0, 2.5) == "kept" and bucket(3.0, 1.5) == "half" and bucket(3.0, 0.5) == "most"
    assert bucket(3.0, -1.0) == "most" and bucket(0.3, 0.2) is None and bucket(None, 1.0) is None


def history(eng):
    """Contract wins: days that kept their run went on overnight; days that gave back most kept falling."""
    for i in range(20):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (30 - i)
        kept = i % 2 == 0
        a.edge = {"exc": {"mfe": 4.0, "mae": -0.5},
                  "outcome": {"ACME": {"sign": 1, "close": 3.5 if kept else 0.5, "d1": 5.0 if kept else -1.0}}}  # fmt: skip
        eng.storage.add_alert(a)


def test_the_overnight_record_by_kind_of_day(tmp_path):
    eng = semis_engine(tmp_path)
    history(eng)
    r = giveback_rates(eng)["contract"]
    assert (
        r["kept"]["n"] == 10
        and r["kept"]["avg"] == round((1.05 / 1.035 - 1) * 100, 2)
        and r["kept"]["hit"] == 1.0
    )
    assert r["most"]["n"] == 10 and r["most"]["avg"] == round((0.99 / 1.005 - 1) * 100, 2)
    v = verdict({"contract": r}, "contract", "most")
    assert v["exit"] and v["line"].startswith(
        "It gave back most of its run today; days like it went -1.5% overnight"
    )
    assert v["line"].endswith(": out at the close instead.")
    assert not verdict({"contract": r}, "contract", "kept")["exit"]
    assert verdict({"contract": r}, "contract", "half") is None  # no record for that kind of day


async def test_the_autopilot_exits_at_the_close_when_it_gave_back_its_run(tmp_path, server):
    from .test_run3 import phones

    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    history(eng)
    eng.refresh_base_rates()
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    rid = eng.desk.get(DEV)["autopilots"][0]["id"]
    close = session_edge("close", time.time(), eng.calendar)
    now = close - 300  # 15:55 ET
    a = news("Acme wins $2B Pentagon contract")
    a.created = close - 5 * 3600
    a.edge = {
        "refs": {"ACME": 100.0},
        "exc": {"mfe": 4.0, "mae": -0.2},
        "catalyst": {"key": "contract", "label": "Contract win"},
    }
    eng.storage.add_alert(a)
    eng.desk.apply(
        DEV, "track_add", {"symbol": "ACME", "direction": "long", "alert_id": a.id, "entry": 100.0}
    )
    t = eng.desk.get(DEV)["tracked"][-1]
    t["auto"], t["exit_at"], t["pb"] = rid, close + 86400, {"out": "d1", "kind": "contract"}
    eng.live_quotes.update("ACME", 100.4, 99.0, now - 5, "tape")  # +0.4% of a 4% run: gave back most
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        assert await eng.autopilot_giveback(close - 3600) == 0  # not yet 15:50
        assert await eng.autopilot_giveback(now) == 1
        done = eng.desk.get(DEV)["closed"][0]
        assert done["lesson"].startswith("autopilot: out at the close: it gave back most of its run today")
        msg = ph.read(next(r for r in server.requests if r["path"] == "/push/a")["body"])
        assert msg["title"] == "🤖 Closed long ACME +0.40% at the close, not tomorrow"
    finally:
        await eng.http.close()
