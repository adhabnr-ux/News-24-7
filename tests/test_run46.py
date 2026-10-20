"""Run 46: playbooks written by the record (everything learned about one kind of news, on one page,
with the rules of thumb its records support) and sound by the call (the push carries the call's verb
so a phone can stay quiet on watch and skip)."""

from __future__ import annotations

import aiohttp
from aiohttp.test_utils import TestServer

from news247.playbook import playbook, playbooks, stocks_for
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run37 import clocked, history
from .test_run38 import graded as month_graded
from .test_run41 import nights


def rich(eng):
    clocked(eng, "Acme wins contract", "ACME", 1, (1.0, 1.2, 1.3, 1.4), 2.0)  # fast
    history(eng)  # overnight contract wins: big gaps faded
    nights(eng, "Acme wins contract", "ACME", 2.0, 2.5)  # up at the close, went on
    month_graded(eng, "Acme wins contract", "ACME", 1.5, 4.2)  # kept drifting
    eng.refresh_base_rates()


def test_a_playbook_from_every_record(tmp_path):
    eng = semis_engine(tmp_path)
    rich(eng)
    p = playbook(eng, "contract")
    assert p["label"] == "Contract win" and p["record"]["close"]["n"] >= 8
    rules = "\n".join(p["rules"])
    assert (
        "Act fast: half its day's move comes within 5 minutes (10); bought 15 minutes in, +0.8% more by the close (10)."
        in rules
    )
    assert "Overnight, don't chase a big gap: gaps of 5%+ kept going after the open 0% (9)." in rules
    assert "Up at the close? Hold it: it went on by the next close 100% (median +0.5%, 10)." in rules
    assert "A month on, it kept drifting: +1.5% at five sessions, +4.2% at twenty (10)." in rules
    assert p["clock"]["half"] == "m5" and p["drift"]["verdict"] == "drifts"
    assert [g["gap"] for g in p["open"]] == ["mid", "big"] and p["overnight"][0]["standing"] == "up"
    assert p["sections"] >= 4
    assert p["stocks"]["best"][0]["symbol"] in ("ACME", "BETA")
    assert playbook(eng, "not_a_kind") is None


def test_a_thin_kind_gets_a_short_page(tmp_path):
    eng = semis_engine(tmp_path)
    eng.refresh_base_rates()
    p = playbook(eng, "fda_approval")
    assert p["rules"] == [] and p["sections"] == 0 and p["stocks"] == {"best": [], "worst": []}


def test_the_index_and_the_stocks(tmp_path):
    eng = semis_engine(tmp_path)
    rich(eng)
    idx = playbooks(eng)["rows"]
    assert idx[0]["key"] == "contract" and idx[0]["n"] >= 8
    st = stocks_for(eng, "contract")
    assert {s["symbol"] for s in st} >= {"ACME"} and all(s["n"] >= 3 for s in st)


async def test_the_push_carries_the_call(tmp_path):
    eng = semis_engine(tmp_path)
    a = news("Acme wins contract")
    a.edge["call"] = {"verb": "watch", "label": "Watch", "line": "👀 The call: watch · a mixed record"}
    assert eng.webpush.payload(a)["call"] == "watch"
    b = news("Acme wins another")
    assert eng.webpush.payload(b)["call"] == ""


async def test_run46_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    rich(eng)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/playbooks"), headers=h) as r:
                assert (await r.json())["rows"][0]["key"] == "contract"
            async with s.get(srv.make_url("/api/playbook/contract"), headers=h) as r:
                assert (await r.json())["clock"]["half"] == "m5"
            async with s.get(srv.make_url("/api/playbook/nope"), headers=h) as r:
                assert r.status == 404
    finally:
        await srv.close()
        await eng.http.close()
