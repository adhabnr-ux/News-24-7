"""Run 76: blind spots by market conditions (the regime, today's tape once it's known, macro days) and
the playbook in dollars (the track record as a compounded account, month by month)."""

from __future__ import annotations

import aiohttp
from aiohttp.test_utils import TestServer

from news247.blindspots import blind_spots, hit, slices, tape_known
from news247.pbcurve import dollars, money_line
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run60 import et
from .test_run64 import pair

# --------------------------------------------------------------------------- market conditions


def test_the_market_slices():
    a = {**news("Acme wins contract").to_dict(), "created": et(14, 11),
         "edge": {"regime": "risk-off", "macro": {"what": "FOMC"}, "event_night": False}}  # fmt: skip
    sl = slices(a, "cold")
    assert sl["regime"] == "risk-off" and sl["macro"] == "a Fed or jobs day" and sl["tape"] == "a cold tape"
    plain = {**a, "edge": {"event_night": False}}
    assert (
        slices(plain)["macro"] == "an ordinary day"
        and "regime" not in slices(plain)
        and "tape" not in slices(plain)
    )
    assert "macro" not in slices(
        {**a, "edge": {}}
    )  # alerts from before macro days were noted aren't sliced by it


def test_a_risk_off_blind_spot(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(60):  # the playbook makes money, except on risk-off days, in both halves
        a = news(f"Acme wins Pentagon contract {i}")
        off = i % 2 == 0
        a.created = et(14, 11) - 86400 * (60 - i)
        a.edge = {**pair("alert", "close"), "refs": {"ACME": 100.0}, "regime": "risk-off" if off else "risk-on",
                  "outcome": {"ACME": {"sign": 1, "close": -1.0 if off else 3.0}}}  # fmt: skip
        eng.storage.add_alert(a)
    r = blind_spots(eng, days=400, now=et(14, 18))
    assert [s["label"] for s in r["spots"]] == ["The market: risk-off"]
    new = {**news("Acme wins contract").to_dict(), "created": et(14, 11), "edge": {"regime": "risk-off"}}
    assert hit(r, new)["value"] == "risk-off"


def test_the_tape_only_once_it_was_known(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(10):  # a session whose first eight alerts lost their first hour: a cold tape
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = et(13, 10) + 300 * i
        a.edge = {"outcome": {"ACME": {"sign": 1, "h1": -1.0, "close": -1.0}}}
        eng.storage.add_alert(a)
    known = tape_known(eng, et(13, 0), et(13, 23))
    st, since = known["2026-10-13"]
    assert st == "cold" and since == et(13, 10) + 300 * 7 + 3600  # the 8th alert's hour


# --------------------------------------------------------------------------- the playbook in dollars


def test_an_account_compounded():
    t = et(14, 11)
    rows = [(t, 10.0, 5.0), (t + 86400, -10.0, 0.0), (t + 86400 * 30, 10.0, 5.0)]
    d = dollars(rows, 10_000.0)
    # a tenth of the balance in each: 10,000 → 10,100 → 9,999 → 10,098.99
    assert d["playbook"] == 10_098.99 and d["plain"] == round(10_000 * 1.005 * 1.005, 2)
    assert [m["month"] for m in d["months"]] == ["2026-10", "2026-11"] and d["drawdown"] == -101.0
    assert d["best"]["month"] == "2026-11" and d["worst"]["month"] == "2026-10"
    assert money_line(d) == ("$10,000 → $10,099 on the playbook (the plain way $10,100); best month +$100, "
                             "worst -$1; its deepest drawdown -$101.")  # fmt: skip


async def test_your_account_size(tmp_path):
    from .test_run73 import graded

    eng = semis_engine(tmp_path)
    for i, d1 in enumerate((3.0, -2.0, 4.0)):
        graded(eng, i, d1, **pair("alert", "d1"))
    eng.desk.apply(DEV, "prefs", {"account": 25000})
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            h = {"Authorization": "Bearer tok", "X-Device": DEV}
            async with s.get(srv.make_url("/api/playbook-curve"), headers=h) as r:
                body = await r.json()
                assert body["money"]["start"] == 25000 and body["money_line"].startswith("$25,000 → $")
            async with s.get(
                srv.make_url("/api/playbook-curve"), headers={"Authorization": "Bearer tok"}
            ) as r:
                assert (await r.json())["money"]["start"] == 10000
    finally:
        await srv.close()
        await eng.http.close()
