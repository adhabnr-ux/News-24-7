"""Run 53: the second leg (a news stock breaking to a new high its way an hour or more after the alert,
after a rest: stamped, pushed, graded from the breakout to the close) and market-wide news graded by
theme (an ETF basket priced at tariff, Fed, oil and war headlines, graded an hour on and at the close)."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247 import macrotape, secondleg
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones

ET = ZoneInfo("America/New_York")


def et(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ET).timestamp()


# --------------------------------------------------------------------------- the second leg


def morning_alert(eng, created=None):
    a = news("Acme wins $2B Pentagon contract")
    a.created = created or et(14, 10)
    a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1}}}
    eng.storage.add_alert(a)
    return a


def test_a_second_leg_needs_a_rest_and_a_new_high(tmp_path):
    eng = semis_engine(tmp_path)
    a = morning_alert(eng)
    eng.live_quotes.update("ACME", 104.0, 99.0, et(14, 10, 20), "tape")  # the first leg: the day's high
    eng.live_quotes.update("ACME", 102.0, 99.0, et(14, 11, 5), "tape")
    assert (
        secondleg.check(
            eng,
            et(
                14,
                11,
                5,
            ),
        )
        == []
    )  # first sight: notes the high (104 at 10:20)
    eng.live_quotes.update("ACME", 103.5, 99.0, et(14, 11, 30), "tape")
    assert secondleg.check(eng, et(14, 11, 30)) == []  # under the high
    eng.live_quotes.update("ACME", 104.6, 99.0, et(14, 11, 40), "tape")
    (g,) = secondleg.check(eng, et(14, 11, 40))  # a new high after 80 minutes without one
    assert (g["alert"]["id"], g["px"], g["its"], g["rest_min"], g["age_min"]) == (a.id, 104.6, 4.6, 80, 100)
    eng.live_quotes.update("ACME", 104.9, 99.0, et(14, 11, 45), "tape")
    assert secondleg.check(eng, et(14, 11, 45)) == []  # one long climb: no rest since the last high
    assert secondleg.check(eng, et(14, 16, 30)) == []  # after the close


def test_a_young_alert_has_no_second_leg(tmp_path):
    eng = semis_engine(tmp_path)
    morning_alert(eng, et(14, 11))
    eng.live_quotes.update("ACME", 104.0, 99.0, et(14, 11, 20), "tape")
    assert secondleg.check(eng, et(14, 11, 30)) == []  # under an hour old: not even read


async def test_the_second_leg_is_stamped_and_pushed(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    eng.leg_rates = {"*": {"key": "*", "label": "Second legs", "n": 23, "hit": 0.57, "median": 1.1}}
    a = morning_alert(eng)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        eng.live_quotes.update("ACME", 104.0, 99.0, et(14, 10, 20), "tape")
        eng.live_quotes.update("ACME", 102.0, 99.0, et(14, 11, 5), "tape")
        await secondleg.send_legs(eng, et(14, 11, 5))
        eng.live_quotes.update("ACME", 104.6, 99.0, et(14, 11, 40), "tape")
        assert await secondleg.send_legs(eng, et(14, 11, 40)) == 1
        msg = ph.read(server.requests[0]["body"])
        assert msg["title"] == "🚀 ACME second leg: a new high 1.7 h after its news"
        assert msg["body"].endswith("A new day's high after 80 minutes without one: +4.6% its way since the alert. "
                                    "Second legs went on by the close 57% (median +1.1%, 23).")  # fmt: skip
        assert eng.storage.get_alert(a.id)["edge"]["leg2"]["px"] == 104.6
    finally:
        await eng.http.close()


def test_second_legs_are_graded(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(8):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (i + 2)
        a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"sign": 1, "close": 6.0 if i < 6 else 2.0}},
                  "leg2": {"px": 104.0, "ts": a.created + 7200}}  # fmt: skip
        eng.storage.add_alert(a)
    one = {
        "edge": {
            "refs": {"ACME": 100.0},
            "outcome": {"ACME": {"sign": 1, "close": 6.0}},
            "leg2": {"px": 104.0},
        }
    }
    assert secondleg.graded(one) == 1.92  # 106 at the close from a 104 breakout
    r = secondleg.leg_rates(eng)
    assert (r["contract"]["n"], r["contract"]["hit"], r["contract"]["median"]) == (8, 0.75, 1.92)
    assert r["contract"]["label"] == "Second legs on contract win news"
    assert secondleg.leg_for(r, "fda_approval") is r["*"]


# --------------------------------------------------------------------------- market-wide news


def macro_alert(eng, title="Trump announces 50% tariffs on chips", theme="trade_war", created=None):
    a = news(title, tickers=())
    a.analysis.themes = [theme]
    if created:
        a.created = created
    return a


def test_market_wide_headlines_price_the_basket(tmp_path):
    eng = semis_engine(tmp_path)
    t0 = et(14, 11)
    for sym, px in (("SPY", 600.0), ("SMH", 250.0), ("XLU", 80.0)):
        eng.live_quotes.update(sym, px, px, t0 - 5, "tape")
    a = macro_alert(eng, created=t0)
    assert macrotape.theme_of(a) == "trade_war" and macrotape.theme_of(a.to_dict()) == "trade_war"
    refs = macrotape.stamp(eng, a, t0)
    assert refs == {"SPY": 600.0, "SMH": 250.0, "XLU": 80.0}
    assert macrotape.stamp(eng, news("Acme wins contract"), t0) == {}  # not market-wide
    a.edge = {"etf_refs": refs}
    eng.storage.add_alert(a)
    eng.live_quotes.update("SPY", 597.0, 600.0, t0 + 3620, "tape")
    eng.live_quotes.update("SMH", 245.0, 250.0, t0 + 3620, "tape")
    eng.live_quotes.update("XLU", 80.4, 80.0, t0 + 3620, "tape")
    assert macrotape.grade(eng, t0 + 3660) == 1
    out = eng.storage.get_alert(a.id)["edge"]["etf_out"]
    assert out == {"SPY": {"h1": -0.5}, "SMH": {"h1": -2.0}, "XLU": {"h1": 0.5}}


def test_the_theme_record(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(6):
        a = macro_alert(eng, f"Tariff headline {i}", created=time.time() - 86400 * (i + 2))
        a.edge = {"etf_out": {"SPY": {"h1": -0.5, "close": -1.0}, "SMH": {"h1": -2.0 if i < 5 else 0.5, "close": -3.0},
                              "XLU": {"h1": 0.3, "close": 0.2}}}  # fmt: skip
        eng.storage.add_alert(a)
    r = macrotape.theme_rates(eng)["trade_war"]
    assert r["n"] == 6 and r["etfs"]["SMH"]["h1"] == {"n": 6, "up": 0.17, "median": -2.0}
    assert r["etfs"]["SMH"]["vs_spy"]["median"] == -1.5
    assert [m["symbol"] for m in macrotape.movers(r)] == ["SMH", "SPY", "XLU"]
    assert macrotape.theme_line(r) == ("Trade-war headlines here, in the hour: SMH -2.0% (down 83%, 6), SPY -0.5% (down 100%, 6), "
                                       "XLU +0.3% (up 100%, 6)")  # fmt: skip


async def test_a_market_wide_alert_carries_its_themes_record(tmp_path):
    eng = semis_engine(tmp_path)
    eng.theme_rates = {"trade_war": {"theme": "trade_war", "label": "Trade-war", "n": 9,
                                     "etfs": {"SMH": {"h1": {"n": 9, "up": 0.22, "median": -1.1}, "close": None}}}}  # fmt: skip
    eng.live_quotes.update("SPY", 600.0, 600.0, time.time() - 5, "tape")
    a = macro_alert(eng)
    await eng.publish(a, push=False)
    assert a.edge["etf_refs"] == {"SPY": 600.0}
    assert a.edge["theme_rec"]["movers"][0]["symbol"] == "SMH"


async def test_run53_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/legs"), headers=h) as r:
                assert (await r.json())["rows"] == []
            async with s.get(srv.make_url("/api/themes"), headers=h) as r:
                assert (await r.json())["need"] == 5
    finally:
        await srv.close()
        await eng.http.close()
