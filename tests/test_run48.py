"""Run 48: the desk's advice (every panel's verdict on a setting, ranked, one tap each) and sympathy,
learned (peers priced at each HIGH alert, graded, and a record per pair)."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247.advice import advise
from news247.dossier import ticker_dossier
from news247.models import Severity
from news247.peerlearn import followers, grade, pair, pair_line, pair_rates, stamp
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import quotes, semis_engine
from .test_run3 import phones
from .test_run14 import market_open

ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- the desk's advice


def test_the_advice_reads_every_verdict(tmp_path):
    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
    eng.catalyst_rates = {"contract|*": {"close": {"n": 40, "hit": 0.3, "median": -1.0}},
                          "downgrade|*": {"close": {"n": 25, "hit": 0.6, "median": 0.8}},
                          "fda_approval|*": {"close": {"n": 12, "hit": 0.2, "median": -2.0}}}  # fmt: skip
    eng.call_replay = {"verbs": [{"verb": "act", "n": 40, "hit": 0.7, "median": 1.2}]}
    r = advise(eng, DEV)
    keys = [i["key"] for i in r["items"]]
    assert "off:contract" in keys and "follow" in keys
    assert "off:fda_approval" not in keys  # 12 graded: too few to switch a kind off
    off = next(i for i in r["items"] if i["key"] == "off:contract")
    assert off["action"] == "cat_mute" and off["body"] == {"key": "contract", "on": True}
    assert off["why"] == "It went its way 30% by the close here (median -1.0%, 40 graded)."
    # switched off, and its record recovered: switch it back on
    eng.desk.apply(DEV, "cat_mute", {"key": "downgrade", "on": True})
    on = next(i for i in advise(eng, DEV)["items"] if i["key"] == "on:downgrade")
    assert on["body"] == {"key": "downgrade", "on": False} and on["label"] == "Switch on"
    # applied or dismissed: gone
    eng.desk.apply(DEV, "advice_dismiss", {"key": "off:contract"})
    r2 = advise(eng, DEV)
    assert "off:contract" not in [i["key"] for i in r2["items"]] and r2["hidden"] == 1
    eng.desk.apply(DEV, "autopilot_add", {"follow": "call", "min_sev": "HIGH", "exit": "close"})
    assert "follow" not in [i["key"] for i in advise(eng, DEV)["items"]]  # already following it


def test_the_line_and_the_tune_advise_too(tmp_path):
    from .test_run43 import over, shadowed

    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
    shadowed(eng, 22, 2, 1.0)
    shadowed(eng, 8, 2, -0.5)
    over(eng, 20, 0.8)
    over(eng, 10, -0.4)
    line = next(i for i in advise(eng, DEV)["items"] if i["key"] == "line")
    assert line["api"] == "/api/line" and line["body"]["medium"] == eng.cfg.scoring.medium - 5


# --------------------------------------------------------------------------- sympathy, learned


def test_peers_are_priced_and_graded(tmp_path):
    eng = semis_engine(tmp_path)
    t0 = datetime(2026, 10, 14, 10, 0, tzinfo=ET).timestamp()
    for sym, px in (("ACME", 100.0), ("BETA", 50.0), ("DLTA", 10.0)):
        eng.live_quotes.update(sym, px, px, t0 - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    a.created = t0
    refs = stamp(eng, a, t0)
    assert refs == {"BETA": 50.0, "DLTA": 10.0}
    assert stamp(eng, news("Acme guides", sev=Severity.MEDIUM), t0) == {}
    a.edge = {"refs": {"ACME": 100.0}, "peer_refs": refs, "outcome": {"ACME": {"close": 3.0, "sign": 1}}}
    eng.storage.add_alert(a)
    eng.live_quotes.update("BETA", 50.5, 50.0, t0 + 3600 + 20, "tape")
    eng.live_quotes.update("DLTA", 9.9, 10.0, t0 + 3600 + 20, "tape")
    assert grade(eng, t0 + 3600 + 60) == 1
    close = datetime(2026, 10, 14, 15, 59, tzinfo=ET).timestamp()
    eng.live_quotes.update("BETA", 51.0, 50.0, close, "tape")
    eng.live_quotes.update("DLTA", 9.8, 10.0, close, "tape")
    assert grade(eng, close + 120) == 1
    out = eng.storage.get_alert(a.id)["edge"]["peer_out"]
    assert out == {"BETA": {"h1": 1.0, "close": 2.0}, "DLTA": {"h1": -1.0, "close": -2.0}}
    assert grade(eng, close + 300) == 0


def pairs(eng, n=6):
    for i in range(n):
        a = news(f"Acme news {i}")
        a.created = time.time() - 86400 * (i + 2)
        a.edge = {"outcome": {"ACME": {"close": 2.0, "sign": 1}},
                  "peer_out": {"BETA": {"close": 1.5 if i < 5 else -0.5}, "DLTA": {"close": -1.0}}}  # fmt: skip
        eng.storage.add_alert(a)


def test_who_follows_whom(tmp_path):
    eng = semis_engine(tmp_path)
    pairs(eng)
    rates = pair_rates(eng)
    assert rates["ACME>BETA"] == {"lead": "ACME", "peer": "BETA", "n": 6, "hit": 0.83, "median": 1.5}
    assert rates["ACME>DLTA"]["hit"] == 0.0
    assert (
        pair_line(pair(rates, "ACME", "BETA"))
        == "When ACME has news, BETA followed its way by the close 83% of the time here (median +1.5%, 6)."
    )
    assert pair(rates, "ACME", "GAMA") is None and pair_line(None) == ""
    f = followers(rates, "ACME")
    assert [x["peer"] for x in f["follow"]] == ["BETA"] and [x["peer"] for x in f["against"]] == ["DLTA"]
    eng.pair_rates = rates
    assert ticker_dossier(eng, "ACME")["followers"]["follow"][0]["peer"] == "BETA"
    assert ticker_dossier(eng, "BETA")["followers"] is None


async def test_the_sympathy_push_carries_the_pair(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    market_open(eng)
    pairs(eng)
    eng.pair_rates = pair_rates(eng)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        now = time.time()
        eng.desk.apply(DEV, "watch_add", {"symbol": "BETA"})
        eng.storage.add_alert(news("Acme wins $2B Pentagon contract"))
        quotes(eng, now, ACME=6.0, BETA=0.5)
        assert await eng.check_sympathy(now) == 1
        msg = ph.read(server.requests[0]["body"])
        assert "When ACME has news, BETA followed its way by the close 83% of the time here" in msg["body"]
    finally:
        await eng.http.close()


async def test_run48_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    pairs(eng)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/advice"), headers=h) as r:
                assert "items" in await r.json()
            async with s.get(srv.make_url("/api/followers/acme"), headers=h) as r:
                assert (await r.json())["follow"][0]["peer"] == "BETA"
            async with s.get(srv.make_url("/api/advice"), headers={"Authorization": "Bearer tok"}) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()
