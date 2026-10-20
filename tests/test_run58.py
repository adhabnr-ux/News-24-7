"""Run 58: how to enter each kind of news (six entry styles graded side by side on the same alerts) and
the tape today (a cold or hot day read from the first alerts' hour, graded on the rest of the day)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247 import tapeday
from news247.entries import entries, entry_line, entry_rates
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones

ET = ZoneInfo("America/New_York")

# --------------------------------------------------------------------------- how to enter


def test_one_alert_entered_six_ways():
    a = {"edge": {"refs": {"ACME": 100.0},
                  "outcome": {"ACME": {"sign": 1, "close": 4.0, "open": 2.0, "m15": 1.0}},
                  "plan": {"patient": {"limit": 99.0, "dip": 1.0}}, "exc": {"mae": -1.5},
                  "leg2": {"px": 103.0}, "call_now": {"verb": "act", "px": 102.0}, "rvol": {"r15": 7.0}}}  # fmt: skip
    got = entries(a)
    assert got == {"alert": 4.0, "open": round((1.04 / 1.02 - 1) * 100, 2), "patient": round((1.04 / 0.99 - 1) * 100, 2),
                   "leg2": round((1.04 / 1.03 - 1) * 100, 2), "turn": round((1.04 / 1.02 - 1) * 100, 2),
                   "vol15": round((1.04 / 1.01 - 1) * 100, 2)}  # fmt: skip
    quiet = {
        "edge": {
            **a["edge"],
            "rvol": {"r15": 1.5},
            "exc": {"mae": -0.4},
            "call_now": {"verb": "skip", "px": 101.0},
        }
    }
    assert set(entries(quiet)) == {
        "alert",
        "open",
        "leg2",
    }  # no volume, the dip never came, the call turned the other way
    assert entries({"edge": {"outcome": {"ACME": {"sign": 1}}}}) == {}


def test_the_best_way_in_per_kind(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(10):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (i + 2)
        a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1, "close": 2.0 if i < 6 else -1.0}},
                  "leg2": {"px": 100.2} if i < 8 else {}}  # fmt: skip
        eng.storage.add_alert(a)
    r = entry_rates(eng)["contract"]
    assert r["n"] == 10 and r["best"] == "leg2"
    assert r["styles"]["alert"] == {"n": 10, "share": 1.0, "hit": 0.6, "median": 2.0, "avg": 0.8}
    assert r["styles"]["leg2"]["n"] == 8 and r["styles"]["leg2"]["share"] == 0.8
    assert entry_line(r).startswith("How to enter contract win news here: on the second leg (")
    assert entry_line(r).endswith("; at the alert +0.8%.")


async def test_alerts_carry_how_to_enter_their_kind(tmp_path):
    eng = semis_engine(tmp_path)
    eng.entry_rates = {"contract": {"kind": "contract", "label": "Contract win", "n": 30, "best": "leg2",
                                    "styles": {"alert": {"n": 30, "share": 1.0, "hit": 0.55, "median": 0.8, "avg": 0.7},
                                               "leg2": {"n": 12, "share": 0.4, "hit": 0.7, "median": 1.4, "avg": 1.3}}}}  # fmt: skip
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    assert a.edge["entry_rec"]["best"] == "leg2" and set(a.edge["entry_rec"]["styles"]) == {"leg2", "alert"}


# --------------------------------------------------------------------------- the tape today


def day_of(eng, day, first_wins, rest_close, n_rest=6):
    t0 = datetime.combine(day, datetime.min.time(), ET).replace(hour=9, minute=40).timestamp()
    for i in range(8):
        a = news(f"First {day} {i}")
        a.created = t0 + i * 300
        a.edge = {"outcome": {"ACME": {"sign": 1, "h1": 1.0 if i < first_wins else -1.0, "close": 0.5}}}
        eng.storage.add_alert(a)
    for i in range(n_rest):
        a = news(f"Rest {day} {i}")
        a.created = t0 + 3 * 3600 + i * 300
        a.edge = {
            "outcome": {
                "ACME": {"sign": 1, "h1": 0.2, "close": rest_close if i < n_rest - 1 else -rest_close}
            }
        }
        eng.storage.add_alert(a)


def sessions_back(n, end=None):
    d = end or (datetime.now(ET).date() - timedelta(days=1))
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= timedelta(days=1)
    return out


def test_the_tape_record(tmp_path):
    eng = semis_engine(tmp_path)
    days = sessions_back(12)
    for d in days[:6]:
        day_of(eng, d, 2, -1.0)  # cold: the rest of the day lost
    for d in days[6:]:
        day_of(eng, d, 5, 1.0)  # normal: the rest worked
    r = tapeday.tape_rates(eng)
    assert (r["cold"]["sessions"], r["cold"]["n"], r["cold"]["hit"]) == (6, 36, 0.17)
    assert (r["normal"]["sessions"], r["normal"]["hit"]) == (6, 0.83)
    assert "hot" not in r


async def test_a_cold_tape_is_pushed_once(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    today = datetime(2026, 10, 14, tzinfo=ET).date()  # a Wednesday
    for d in sessions_back(12, today - timedelta(days=1))[:6]:
        day_of(eng, d, 2, -1.0)
    for d in sessions_back(12, today - timedelta(days=1))[6:]:
        day_of(eng, d, 5, 1.0)
    now = datetime(2026, 10, 14, 11, 30, tzinfo=ET).timestamp()
    eng.tape_rates = tapeday.tape_rates(eng, 365, now)
    day_of(eng, today, 1, 0.0, n_rest=0)
    t = tapeday.today(eng, now)
    assert (t["state"], t["won"], t["graded"]) == ("cold", 1, 8)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        assert await tapeday.send_tape(eng, now) == 1
        msg = ph.read(server.requests[0]["body"])
        assert msg["title"] == "🧊 A cold tape today"
        assert msg["body"] == ("1 of the first 8 alerts went their way an hour in. On days like it here, the rest of the day's alerts "
                               "went their way 17% by the close (median -1.0%, 6 sessions) vs 83% on normal days.")  # fmt: skip
        assert await tapeday.send_tape(eng, now + 600) == 0  # once a day
    finally:
        await eng.http.close()


def test_no_read_until_the_first_alerts_have_their_hour(tmp_path):
    eng = semis_engine(tmp_path)
    day = datetime(2026, 10, 14, tzinfo=ET).date()
    t0 = datetime(2026, 10, 14, 9, 40, tzinfo=ET).timestamp()
    for i in range(8):
        a = news(f"First {i}")
        a.created = t0 + i * 300
        a.edge = {"outcome": {"ACME": {"sign": 1, **({"h1": 1.0} if i < 5 else {})}}}
        eng.storage.add_alert(a)
    t = tapeday.today(eng, datetime(2026, 10, 14, 10, 50, tzinfo=ET).timestamp())
    assert t["state"] is None and t["graded"] == 5
    assert day.isoformat() == "2026-10-14"


@pytest.mark.parametrize("wins,state", [(3, "cold"), (4, "normal"), (5, "normal"), (6, "hot")])
def test_the_bands(wins, state):
    first = [{"edge": {"outcome": {"A": {"sign": 1, "h1": 1.0 if i < wins else -1.0}}}} for i in range(8)]
    assert tapeday.state_of(first) == state


async def test_run58_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/entries"), headers=h) as r:
                assert (await r.json())["styles"]["leg2"] == "On the second leg"
            async with s.get(srv.make_url("/api/tape"), headers=h) as r:
                assert (await r.json())["need"] == 8
    finally:
        await srv.close()
        await eng.http.close()
