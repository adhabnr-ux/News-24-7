"""Run 45: options against the news (what options price for a day against what this kind of news
usually moves, graded) and the call tuning itself (facts that sort nothing in the replay are muted)."""

from __future__ import annotations

import time

import aiohttp
from aiohttp.test_utils import TestServer

from news247 import personal
from news247.thecall import muted_facts, replay, the_call
from news247.volnews import implied_day, read, stamp_later, vol_line, vol_rates
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run16 import contract_history

IV = {"available": True, "iv30": 30.0, "rows": [{"expiry": "2026-10-23", "dte": 7, "move_pct": 5.0}]}


# --------------------------------------------------------------------------- options against the news


def test_what_options_price_for_a_day():
    assert implied_day(IV) == (1.27, "IV30 30%")
    assert implied_day({**IV, "iv30": None}) == (1.89, "the 2026-10-23 straddle")
    assert implied_day({"available": False}) is None and implied_day(None) is None
    assert (
        implied_day({"available": True, "iv30": None, "rows": [{"expiry": "x", "dte": 0, "move_pct": 2.0}]})
        is None
    )


def test_cheap_fair_or_rich():
    cb = lambda t: {"typical": t, "label": "Contract win"}  # noqa: E731
    cheap = read(IV, cb(4.0))
    assert (cheap["ratio"], cheap["verdict"], cheap["implied"]) == (3.15, "cheap", 1.27)
    assert read(IV, cb(1.0))["verdict"] == "rich" and read(IV, cb(1.5))["verdict"] == "fair"
    assert read(IV, {"label": "x"}) is None and read(None, cb(4.0)) is None
    assert (
        vol_line(cheap)
        == "🎯 Options price ±1.3% a typical day (IV30 30%); contract win news here moves ±4.0% by the close: cheap for it"
    )
    assert vol_line(read(IV, cb(1.5))) == ""  # fair: nothing to say
    assert vol_line(cheap, {"n": 12, "beat": 0.75}).endswith(
        "(on cheap reads here the move beat what options priced 75% of the time, 12)"
    )


def test_each_read_is_graded(tmp_path):
    eng = semis_engine(tmp_path)
    for i, close in enumerate([3.0, -2.5, 4.0, 0.8, -1.9, 2.2]):
        a = news(f"Acme read {i}")
        a.created = time.time() - 86400 * 2 - i * 60
        a.edge = {"vol": {"implied": 1.27, "typical": 4.0, "ratio": 3.15, "verdict": "cheap", "from": "IV30 30%", "label": "x"},
                  "outcome": {"ACME": {"close": close, "sign": 1}}}  # fmt: skip
        eng.storage.add_alert(a)
    r = vol_rates(eng)["cheap"]
    assert r["n"] == 6 and r["beat"] == 0.83 and r["ratio"] == 1.85  # either way: 5 of 6 beat ±1.27%


async def test_a_new_alert_reads_a_fresh_chain(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.refresh_base_rates()
    eng.implied_cache["ACME"] = (time.time(), IV)
    a = news("Acme wins $900M Navy contract")
    await eng.publish(a, push=False)
    v = a.edge["vol"]
    assert (v["implied"], v["typical"], v["ratio"], v["verdict"]) == (1.27, 2.75, 2.17, "cheap")
    assert (
        "🎯 Options price ±1.3% a typical day (IV30 30%); contract win news here moves ±2.8% by the close: cheap for it"
        in eng.webpush.payload(a)["body"]
    )


async def test_a_cold_chain_is_read_after_the_alert(tmp_path, monkeypatch):
    eng = semis_engine(tmp_path)
    a = news("Acme wins contract")
    eng.storage.add_alert(a)

    async def fake(engine, sym, now=None):
        return IV

    monkeypatch.setattr(personal, "implied_move", fake)
    await stamp_later(eng, a.id, "ACME", {"typical": 4.0, "label": "Contract win"})
    assert eng.storage.get_alert(a.id)["edge"]["vol"]["verdict"] == "cheap"


# --------------------------------------------------------------------------- the call tunes itself


def graded(eng, n, close, **edge):
    for i in range(n):
        a = news(f"Acme {close} {i} {sorted(edge)}")
        a.created = time.time() - 86400 * 5 - i * 600
        a.edge = {**edge, "outcome": {"ACME": {"close": close, "sign": 1}}}
        eng.storage.add_alert(a)


def test_a_fact_that_sorts_nothing_is_muted(tmp_path):
    eng = semis_engine(tmp_path)
    late = {"min": 20, "rec": {"close": {"n": 30, "hit": 0.3}}}
    graded(eng, 12, 1.0, odds={"p": 0.66})  # odds for: won
    graded(eng, 12, -1.0, odds={"p": 0.40})  # odds against: lost
    graded(eng, 11, 1.0, late=late)  # late to the story: won more often than not...
    graded(eng, 10, -1.0, late=late)
    graded(eng, 21, -1.0, odds={"p": 0.66}, late={"min": 0})  # (odds for, and not late: lost)
    rp = replay(eng)
    facts = {f["key"]: f for f in rp["facts"]}
    assert facts["late"]["for"] is None  # late only ever speaks against
    assert muted_facts(rp) == []  # nothing with 20+ on each side sorts badly yet
    graded(eng, 25, 1.0, odds={"p": 0.40})  # odds against now won as often as odds for
    rp = replay(eng)
    odds = next(f for f in rp["facts"] if f["key"] == "odds")
    assert odds["sorts"] <= 0 and muted_facts(rp) == ["odds"]
    call = the_call({"odds": {"p": 0.40}, "late": late}, muted=frozenset({"odds"}))
    assert call["against"] == ["late to the story (20 min)"] and call["muted"] == ["odds"]
    assert the_call({"odds": {"p": 0.40}}, muted=frozenset({"odds"})) is None  # nothing left that counts


async def test_run45_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/vol"), headers=h) as r:
                body = await r.json()
                assert body["rates"] == [] and body["today"] == []
            async with s.get(srv.make_url("/api/call"), headers=h) as r:
                assert (await r.json())["muted"] == []
    finally:
        await srv.close()
        await eng.http.close()
