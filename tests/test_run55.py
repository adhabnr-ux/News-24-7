"""Run 55: the odds learn the new records (float, short interest, dilution, volume, lateness, the stock's
personality, macro days, the climate) with what each adds on the held-out test, and the call, live
(re-read every minute with the day's graded facts, stamped, pushed and graded when it turns)."""

from __future__ import annotations

import json
import random
import time

import aiohttp
from aiohttp.test_utils import TestServer

from news247 import callnow
from news247.odds import ablation, features
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones

# --------------------------------------------------------------------------- the odds learn the new records


def test_the_new_records_are_model_inputs():
    a = news("Acme wins $2B Pentagon contract").to_dict()
    a["edge"] = {"float": {"fband": "tiny", "sband": "crowded"}, "dilution": {"state": "selling"}, "rvol": {"r0": 7.0},
                 "late": {"min": 30}, "persona": {"trait": "runs"}, "macro": {"today": ["FOMC"]}, "climate": {"state": "hot"}}  # fmt: skip
    x = features(a, None)
    for k in (
        "float=tiny",
        "short=crowded",
        "dil=selling",
        "vol=heavy",
        "late=late",
        "persona=runs",
        "macroday",
        "climate=hot",
    ):
        assert x[k] == 1.0, k


def test_what_each_record_adds():
    rng = random.Random(3)
    rows = []
    for i in range(400):
        tiny = rng.random() < 0.5
        y = int(rng.random() < (0.8 if tiny else 0.3))  # a tiny float matters
        x = {
            "kind=news": 1.0,
            f"hour={'midday' if rng.random() < 0.5 else 'the first hour'}": 1.0,
        }  # the hour doesn't
        x["float=tiny" if tiny else "float=large"] = 1.0
        rows.append((float(i), x, y))
    from news247.odds import _test_and_fit

    t, _, _ = _test_and_fit(rows)
    adds = {r["name"]: r for r in ablation(rows, t["skill"])}
    assert adds["Float and short interest"]["adds"] > 5 and adds["Float and short interest"]["seen"] == 1.0
    assert abs(adds["Time of day"]["adds"]) < 2
    assert "Dilution risk" not in adds  # on no test alert


async def test_the_odds_read_the_float_at_publish(tmp_path):
    eng = semis_engine(tmp_path)
    eng.odds = {"trusted": True, "test_trusted": True, "n": 300, "test": {"skill": 0.08},
                "model": {"b": 0.0, "w": {"float=tiny": 1.2}, "base": 0.5}}  # fmt: skip
    eng.storage.set_setting("float:ACME", json.dumps({"float": 7_000_000, "short": 0.1, "ts": time.time()}))
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    assert a.edge["odds"]["p"] > 0.7  # stamped before the odds were read
    assert a.edge["odds"]["why"][0]["label"] == "A float under 10M shares"


# --------------------------------------------------------------------------- the call, live


def heavy(eng):
    eng.rvol_rates = {"r15:heavy|*": {"key": "r15:heavy|*", "when": "r15", "band": "heavy", "kind": "*", "n": 12, "hit": 0.75,
                                      "median": 2.0, "label": "News on 5-10x normal volume 15 minutes in"},
                      "r15:quiet|*": {"key": "r15:quiet|*", "when": "r15", "band": "quiet", "kind": "*", "n": 12, "hit": 0.3,
                                      "median": -1.0, "label": "News on under 2x normal volume 15 minutes in"}}  # fmt: skip


def watched(eng, r15, call="watch", created=None):
    a = news("Acme wins $2B Pentagon contract")
    a.created = created or time.time() - 1200
    a.edge = {"refs": {"ACME": 100.0}, "rvol": {"r0": 3.0, "r15": r15},
              "call": {"verb": call, "label": {"watch": "Watch", "act": "Act now"}[call], "line": f"The call: {call}", "for": [], "against": []},
              "odds": {"p": 0.62}}  # fmt: skip
    eng.storage.add_alert(a)
    return a


def test_live_facts_and_the_call_now(tmp_path):
    eng = semis_engine(tmp_path)
    heavy(eng)
    a = watched(eng, 7.5)
    facts = callnow.live_facts(eng, eng.storage.get_alert(a.id))
    assert facts == [("+", "vol15", "7.5x volume 15 minutes in: 75%")]
    c = callnow.call_now(eng, eng.storage.get_alert(a.id))
    assert c["verb"] == "act" and c["live_facts"] == [["+", "vol15", "7.5x volume 15 minutes in: 75%"]]
    b = watched(eng, 1.2, call="act")
    assert (
        callnow.call_now(eng, eng.storage.get_alert(b.id))["verb"] == "watch"
    )  # one for (odds), one against
    assert callnow.live_facts(eng, {"edge": {"rvol": {"r15": 3.0}}}) == []  # no record for that band


async def test_a_turn_is_stamped_pushed_and_graded(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    heavy(eng)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        now = time.time()
        eng.live_quotes.update("ACME", 102.0, 99.0, now - 5, "tape")
        a = watched(eng, 7.5)
        assert await callnow.send_turns(eng, now) == 1
        cn = eng.storage.get_alert(a.id)["edge"]["call_now"]
        assert (cn["verb"], cn["from"], cn["px"], cn["live"]) == (
            "act",
            "watch",
            102.0,
            ["7.5x volume 15 minutes in: 75%"],
        )
        msg = ph.read(server.requests[0]["body"])
        assert msg["title"] == "▶ ACME: the call is now act now (was watch)"
        assert await callnow.send_turns(eng, now + 60) == 0  # it stands as changed: no second push
    finally:
        await eng.http.close()
    eng.storage.update_alert_edge(a.id, {"outcome": {"ACME": {"sign": 1, "close": 4.04}}})
    assert callnow.graded(eng.storage.get_alert(a.id)) == ("act", 2.0)  # 104.04 at the close from a 102 turn


def test_turns_are_graded(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(8):
        a = news(f"Acme news {i}")
        a.created = time.time() - 86400 * (i + 2)
        a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1, "close": 3.0 if i < 6 else -1.0}},
                  "call_now": {"verb": "act", "px": 101.0}}  # fmt: skip
        eng.storage.add_alert(a)
    r = callnow.turn_rates(eng)
    assert (r["act"]["n"], r["act"]["hit"], r["act"]["median"]) == (8, 0.75, 1.98)


async def test_run55_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s, s.get(srv.make_url("/api/call"), headers=h) as r:
            assert (await r.json())["turns"] == []
    finally:
        await srv.close()
        await eng.http.close()


def test_what_each_record_adds_is_kept_a_day(tmp_path, monkeypatch):
    from news247 import odds

    calls = []
    monkeypatch.setattr(
        odds, "ablation", lambda rows, skill: calls.append(1) or [{"name": "x", "adds": 1.0, "seen": 1.0}]
    )
    monkeypatch.setattr(
        odds,
        "_rows",
        lambda *a, **k: [(float(i), {"kind=news": 1.0, f"f{i % 2}": 1.0}, i % 2) for i in range(200)],
    )
    eng = semis_engine(tmp_path)
    first = odds.fit_odds(eng, now=1000.0)
    again = odds.fit_odds(eng, now=1000.0 + 3600, prev=first)
    assert len(calls) == 1 and again["adds"] == first["adds"] and again["adds_at"] == 1000.0
    odds.fit_odds(eng, now=1000.0 + 86400 + 1, prev=again)
    assert len(calls) == 2
