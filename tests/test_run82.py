"""Run 82: the learning system walked forward (each month of history traded with only the picks known
before it) and the lookback, learned (the picker's window switches only when another beat it in both
halves of the walk-forward record)."""

from __future__ import annotations

import time
from datetime import datetime

import aiohttp
from aiohttp.test_utils import TestServer

from news247.walkforward import MIN_SHARED, _beats, replay, walk_forward
from news247.web.server import WebServer

from .test_desk import news
from .test_personal import semis_engine
from .test_run60 import ET


def row(i, month, d1, close=1.0, key="contract", day=5):
    created = datetime(2026, month, day, 11, tzinfo=ET).timestamp()
    from news247.walkforward import SETTLE, _month

    return {"id": f"a{month}-{i}", "created": created, "month": _month(created), "seen": _month(created + SETTLE),
            "key": key, "got": {"alert|close": close, "alert|d1": d1}}  # fmt: skip


def test_each_month_traded_with_what_was_known_before_it():
    rs = [row(i, 3, 3.0) for i in range(10)] + [row(i, 4, -1.0) for i in range(10)]
    res = replay(rs, None)
    assert not any(k.startswith("a3-") for k in res)  # nothing graded before March: no picks, no trades
    april = [v for k, v in res.items() if k.startswith("a4-")]
    # April is traded with March's pick (out the next day, +3%), and loses on April's own turn
    assert len(april) == 10 and {v["pick"] for v in april} == {"alert|d1"} and april[0]["pb"] == -1.0
    assert april[0]["plain"] == 1.0


def test_late_in_the_month_waits_for_its_grades():
    rs = [row(i, 3, 3.0, day=28) for i in range(10)] + [row(i, 4, -1.0) for i in range(10)]
    assert not replay(rs, None)  # March 28 + 8 days is April: not known at April's start


def test_a_losing_kind_passes():
    rs = [row(i, 3, -2.0, close=-1.0) for i in range(10)] + [row(i, 4, 5.0) for i in range(10)]
    assert {v["pb"] for v in replay(rs, None).values()} == {0.0}  # even the best pair lost: it sat April out


def test_a_short_lookback_forgets():
    old = [row(i, 1, 3.0) for i in range(20)]  # January: out the next day paid
    recent = [row(i, 5, -1.0, close=2.0) for i in range(10)]  # May: it stopped paying; the close did
    june = [row(i, 6, 0.0) for i in range(10)]
    long, short = replay(old + recent + june, None), replay(old + recent + june, 3)
    assert {long[k]["pick"] for k in long if k.startswith("a6-")} == {
        "alert|d1"
    }  # all history: still January's
    assert {short[k]["pick"] for k in short if k.startswith("a6-")} == {
        "alert|close"
    }  # the last 3 months: May's


def test_beating_in_both_halves():
    def res(xs):
        return {f"a{i}": {"created": float(i), "pb": x} for i, x in enumerate(xs)}

    n = MIN_SHARED
    better, worse = res([1.0] * n), res([0.5] * n)
    assert _beats(better, worse) and not _beats(worse, better)
    lucky = res([0.0] * (n // 2) + [3.0] * (n // 2))  # only the newer half: not both
    assert not _beats(lucky, res([0.5] * n))
    assert not _beats(res([1.0] * 50), res([0.5] * 50))  # too few in common


def graded_history(eng, months=4, per=12):
    for m in range(months):
        for i in range(per):
            a = news(f"Acme wins Pentagon contract {m} {i}")
            a.created = time.time() - 86400 * (30 * (months - m) + 10 - i * 0.1)
            a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1, "close": 1.0, "d1": 2.0}}}
            eng.storage.add_alert(a)


def test_the_record_and_its_line(tmp_path):
    eng = semis_engine(tmp_path)
    graded_history(eng)
    r = walk_forward(eng)
    w = r["windows"]["6m"]
    assert r["pick"] == "6m" and w["n"] > 0 and w["avg"] == 2.0 and w["plain_avg"] == 1.0
    assert r["line"].startswith(
        f"Walked forward since {w['since']}, on {w['n']} alerts each traded with only what was known"
    )
    assert r["line"].endswith("It looks back over the last 6 months.")


async def test_the_picker_uses_the_lookback_it_learned(tmp_path):
    eng = semis_engine(tmp_path)
    graded_history(eng, months=5)
    eng.storage.set_setting("lookback", "3m")  # learned earlier
    eng.refresh_base_rates()
    assert eng.storage.get_setting("lookback") == "3m"  # nothing beat it in both halves: it stays
    assert eng.combo_rates["*"]["n"] < 60  # only the last 90 days picked from
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with (
            aiohttp.ClientSession() as s,
            s.get(srv.make_url("/api/walkforward"), headers={"Authorization": "Bearer tok"}) as resp,
        ):
            body = await resp.json()
            assert body["current"] == "3m" and set(body["windows"]) == {"all", "6m", "3m"}
    finally:
        await srv.close()
        await eng.http.close()
