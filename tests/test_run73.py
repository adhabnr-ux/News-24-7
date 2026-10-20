"""Run 73: event risk on overnight holds (the stock's own earnings or a binary event tonight: noted on
the alert, graded separately, and no hold through one until the record says it pays) and the
playbook's track record as a curve (every stamped alert, the playbook's way against the plain way)."""

from __future__ import annotations

import time
from datetime import date
from types import SimpleNamespace

from news247.desk import session_edge
from news247.eventrisk import MIN_N, night_rates, tonight, verdict
from news247.pbcurve import curve, line, taken

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run60 import et
from .test_run64 import pair


def book(**reports):
    """A stub of the earnings calendar: {symbol: (date, when)}."""

    def lookup(sym, today):
        r = reports.get(sym)
        if not r or date.fromisoformat(r[0]) < today:
            return None
        return {"date": r[0], "when": r[1], "in_days": (date.fromisoformat(r[0]) - today).days}

    return SimpleNamespace(earnings=SimpleNamespace(lookup=lookup))


# --------------------------------------------------------------------------- event risk on holds


def test_which_nights_are_event_nights(tmp_path):
    eng = semis_engine(tmp_path)
    t = et(14, 11)  # Wednesday Oct 14, 11:00 ET
    eng.trends = book(ACME=("2026-10-14", "after the close"), BETA=("2026-10-15", "before the open"),
                      DLTA=("2026-10-15", "after the close"), GAMA=("2026-10-16", ""))  # fmt: skip
    assert tonight(eng, "ACME", t)["line"] == "ACME reports earnings after the close"
    assert tonight(eng, "BETA", t)["what"] == "earnings"
    assert tonight(eng, "DLTA", t) is None  # tomorrow after the close: tomorrow night's problem
    assert tonight(eng, "GAMA", t) is None
    eng.storage.add_catalyst(
        {"id": "ev1", "date": "2026-10-15", "symbol": "GAMA", "title": "PDUFA date for Gama's drug"}
    )
    assert tonight(eng, "GAMA", t)["what"] == "event"
    friday = et(16, 11)  # Friday: the next session is Monday
    eng.trends = book(ACME=("2026-10-19", "before the open"))
    assert tonight(eng, "ACME", friday)["date"] == "2026-10-19"


def test_no_hold_through_an_event_until_the_record_says_so(tmp_path):
    eng = semis_engine(tmp_path)
    a = {
        "created": et(14, 11),
        "edge": {"event_night": {"what": "earnings", "line": "ACME reports earnings after the close"}},
    }
    v = verdict(eng, a, "ACME")
    assert v["exit"] and v["line"] == ("ACME reports earnings after the close: the playbook doesn't hold through one until its "
                                       "record says it pays. Out at the close instead.")  # fmt: skip
    assert (
        verdict(eng, {"created": et(14, 11), "edge": {"event_night": False}}, "ACME") is None
    )  # an ordinary night
    eng.night_rates = {"event": {"n": MIN_N, "avg": 1.4, "hit": 0.6, "abs": 5.0}}
    assert verdict(eng, a, "ACME")["exit"] is False and "the hold stands" in verdict(eng, a, "ACME")["line"]


def test_the_event_night_record(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(10):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (20 - i)
        a.edge = {"event_night": {"what": "earnings"} if i % 2 else False,
                  "outcome": {"ACME": {"sign": 1, "close": 1.0, "d1": -4.0 if i % 2 else 2.0}}}  # fmt: skip
        eng.storage.add_alert(a)
    r = night_rates(eng)
    assert (
        r["event"]["n"] == 5
        and r["event"]["avg"] == round((0.96 / 1.01 - 1) * 100, 2)
        and r["normal"]["hit"] == 1.0
    )


async def test_the_autopilot_doesnt_hold_through_earnings(tmp_path, server):
    from .test_run3 import phones

    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    rid = eng.desk.get(DEV)["autopilots"][0]["id"]
    close = session_edge("close", time.time(), eng.calendar)
    a = news("Acme wins $2B Pentagon contract")
    a.created = close - 5 * 3600
    a.edge = {
        "refs": {"ACME": 100.0},
        "event_night": {"what": "earnings", "line": "ACME reports earnings after the close"},
    }
    eng.storage.add_alert(a)
    eng.desk.apply(
        DEV, "track_add", {"symbol": "ACME", "direction": "long", "alert_id": a.id, "entry": 100.0}
    )
    t = eng.desk.get(DEV)["tracked"][-1]
    t["auto"], t["exit_at"], t["pb"] = rid, close + 86400, {"out": "d1", "kind": "contract"}
    eng.live_quotes.update("ACME", 102.0, 99.0, close - 305, "tape")
    await eng.http.start()
    try:
        phones(eng, server)
        assert await eng.autopilot_giveback(close - 300) == 1
        assert "reports earnings after the close" in eng.desk.get(DEV)["closed"][0]["lesson"]
    finally:
        await eng.http.close()


async def test_publish_notes_the_night(tmp_path):
    eng = semis_engine(tmp_path)
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    assert eng.storage.get_alert(a.id)["edge"]["event_night"] is False  # checked: an ordinary night


# --------------------------------------------------------------------------- the track record


def graded(eng, i, d1, close=1.0, **edge):
    a = news(f"Acme wins Pentagon contract {i}")
    a.created = time.time() - 86400 * (30 - i)
    a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1, "close": close, "d1": d1}}, **edge}
    eng.storage.add_alert(a)
    return a


def test_what_the_playbook_took(tmp_path):
    eng = semis_engine(tmp_path)
    a = graded(eng, 0, 3.0, **pair("alert", "d1"))
    assert taken(eng.storage.get_alert(a.id)) == (3.0, 1.0)
    p = pair("alert", "d1")
    p["combo_rec"]["pass"] = True
    b = graded(eng, 1, 3.0, **p)
    assert taken(eng.storage.get_alert(b.id)) == (0.0, 1.0)  # it sat this one out
    c = graded(eng, 2, 3.0)
    assert taken(eng.storage.get_alert(c.id)) is None  # not stamped: not the playbook's


def test_the_curve(tmp_path):
    eng = semis_engine(tmp_path)
    for i, d1 in enumerate((3.0, -2.0, 4.0, 1.0)):
        graded(eng, i, d1, **pair("alert", "d1"))
    c = curve(eng)
    assert c["n"] == 4 and c["playbook"]["total"] == 6.0 and c["plain"]["total"] == 4.0 and c["taken"] == 4
    assert [p[1] for p in c["points"]] == [3.0, 1.0, 5.0, 6.0] and c["drawdown"] == -2.0
    assert line(c).startswith(
        f"Since {c['since']}, on 4 alerts: the playbook +6.0% in all (+1.50% an alert), the plain way +4.0%"
    )
    assert curve(semis_engine(tmp_path / "b")) == {"n": 0}
