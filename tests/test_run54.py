"""Run 54: volume confirmation graded (relative volume at the alert and 15 minutes in, by band and kind,
a fact in the call, a push 15 minutes in when the band's record is clear) and your book on market-wide
news (holdings read through their sector ETFs against the theme's record, pushed to holders)."""

from __future__ import annotations

import time
from types import SimpleNamespace

import aiohttp
from aiohttp.test_utils import TestServer

from news247 import bookmacro, volconfirm
from news247.thecall import the_call
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones

# --------------------------------------------------------------------------- volume confirmation


class Tape:
    def __init__(self, rv):
        self.rv = rv

    def latest(self, sym, now=None, max_age_s=900.0):
        v = self.rv.get(sym)
        return {"rvol": v, "ts": now} if v is not None else None


def test_reading_relative_volume(tmp_path):
    eng = semis_engine(tmp_path)
    eng.ignition = Tape({"ACME": 7.24})
    assert volconfirm.read(eng, "ACME") == 7.2 and volconfirm.read(eng, "BETA") is None
    eng.ignition = SimpleNamespace(
        latest=lambda s, now=None: {"rvol": 3.0}
    )  # an older tape without max_age_s
    assert volconfirm.read(eng, "ACME") == 3.0
    eng.ignition = None
    assert volconfirm.read(eng, "ACME") is None
    assert [volconfirm.band(x) for x in (1.0, 2.0, 7.0, 25.0, None)] == [
        "quiet",
        "busy",
        "heavy",
        "frenzy",
        None,
    ]


def voted(eng, n, r15, close, r0=None):
    for i in range(n):
        a = news(f"Acme wins Pentagon contract {r15} {close} {i}")
        a.created = time.time() - 86400 * (i + 2)
        rv = {"r15": r15}
        if r0 is not None:
            rv["r0"] = r0
        a.edge = {"rvol": rv, "outcome": {"ACME": {"sign": 1, "close": close}}}
        eng.storage.add_alert(a)


def test_news_by_volume(tmp_path):
    eng = semis_engine(tmp_path)
    voted(eng, 6, 7.0, 3.0, r0=4.0)
    voted(eng, 2, 6.0, -1.0, r0=4.0)
    voted(eng, 8, 1.2, -0.5, r0=1.0)
    r = volconfirm.rvol_rates(eng)
    h = r["r15:heavy|contract"]
    assert (h["n"], h["hit"], h["median"]) == (8, 0.75, 3.0) and h[
        "label"
    ] == "Contract win on 5-10x normal volume 15 minutes in"
    assert r["r0:busy|*"]["label"] == "News on 2-5x normal volume when it went out"
    assert volconfirm.rvol_for(r, "contract", 8.0) is h
    assert volconfirm.rvol_for(r, "fda_approval", 1.5) is r["r15:quiet|*"]
    assert volconfirm.rvol_for(r, "contract", 25.0) is None
    b = volconfirm.board(r)
    assert [x["band"] for x in b["rows"]] == ["quiet", "heavy"]


def test_the_call_reads_the_volume():
    rec = {"label": "News on 2-5x normal volume when it went out", "n": 12, "hit": 0.3, "median": -1.0}
    c = the_call({"rvol_base": rec})
    assert c["verb"] == "skip" and c["against"] == ["news on 2-5x normal volume when it went out 30%"]


async def test_fifteen_minutes_in(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    voted(eng, 8, 7.0, 3.0)
    eng.refresh_base_rates()
    eng.ignition = Tape({"ACME": 7.5})
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        now = time.time()
        a = news("Acme wins $2B Pentagon contract")
        a.created = now - 960
        a.edge = {"rvol": {"r0": 3.1}}
        eng.storage.add_alert(a)
        young = news("Acme raises guidance")
        young.created = now - 300
        eng.storage.add_alert(young)
        assert await volconfirm.send_confirms(eng, now) == 1
        assert eng.storage.get_alert(a.id)["edge"]["rvol"] == {"r0": 3.1, "r15": 7.5}
        assert "rvol" not in (eng.storage.get_alert(young.id).get("edge") or {})  # not 15 minutes old yet
        msg = ph.read(server.requests[0]["body"])
        assert msg["title"] == "📊 ACME 15 minutes in: confirmed by volume"
        assert msg["body"].endswith("7.5x its normal volume 15 minutes in. Contract win on 5-10x normal volume 15 minutes in: "
                                    "went its way 100% by the close (median +3.0%, 8).")  # fmt: skip
        assert await volconfirm.send_confirms(eng, now + 60) == 0  # once
    finally:
        await eng.http.close()


async def test_alerts_carry_their_volume(tmp_path):
    eng = semis_engine(tmp_path)
    voted(eng, 9, 3.0, 2.0, r0=3.0)
    eng.refresh_base_rates()
    eng.ignition = Tape({"ACME": 3.4})
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    assert a.edge["rvol"] == {"r0": 3.4} and a.edge["rvol_base"]["key"] == "r0:busy|contract"
    assert "rvol" in [k for k, _ in a.edge["call"]["facts"]]


# --------------------------------------------------------------------------- your book on market-wide news


def trade_war(eng):
    eng.theme_rates = {"trade_war": {"theme": "trade_war", "label": "Trade-war", "n": 9, "etfs": {
        "SMH": {"h1": {"n": 9, "up": 0.22, "median": -2.0}}, "XLF": {"h1": {"n": 9, "up": 0.4, "median": -0.4}},
        "SPY": {"h1": {"n": 9, "up": 0.3, "median": -0.6}}}}}  # fmt: skip


def test_the_book_is_read_through_its_sectors(tmp_path):
    eng = semis_engine(tmp_path)
    trade_war(eng)
    assert bookmacro.etf_for(eng, "ACME") == "SMH"  # Semiconductors
    assert bookmacro.etf_for(eng, "ZZZZ") == "SPY"
    eng.desk.apply(DEV, "hold_set", {"symbol": "ACME", "shares": 100, "cost": 90})
    eng.desk.apply(DEV, "hold_set", {"symbol": "ZZZZ", "shares": 10, "cost": 50})
    eng.live_quotes.update("ACME", 100.0, 99.0, time.time() - 5, "tape")
    est = bookmacro.estimate(eng, DEV, "trade_war")
    assert est["value"] == 10500.0 and est["dollars"] == -203.0 and est["pct"] == -1.93
    assert [(r["symbol"], r["etf"], r["dollars"]) for r in est["rows"]] == [
        ("ACME", "SMH", -200.0),
        ("ZZZZ", "SPY", -3.0),
    ]
    assert bookmacro.line(est) == ("Your book moved -$203 (-1.9%) in the hour after trade-war headlines like it (9), by its "
                                   "sectors' records: SMH -2.0% for ACME; SPY -0.6% for ZZZZ.")  # fmt: skip
    assert bookmacro.estimate(eng, DEV, "fed_policy") is None  # no record for that theme
    assert bookmacro.estimate(eng, "nobody", "trade_war") is None


async def test_holders_get_their_books_estimate(tmp_path, server):
    from news247.macrotape import theme_of

    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = semis_engine(tmp_path)
    trade_war(eng)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "hold_set", {"symbol": "ACME", "shares": 100, "cost": 90})
        eng.live_quotes.update("ACME", 100.0, 99.0, time.time() - 5, "tape")
        a = news("Trump announces 50% tariffs on chips", tickers=())
        a.analysis.themes = ["trade_war"]
        eng.storage.add_alert(a)
        assert theme_of(a.to_dict()) == "trade_war"
        assert await bookmacro.send_book(eng, a.to_dict()) == 1
        msg = ph.read(server.requests[0]["body"])
        assert msg["title"] == "🌐 Trade-war headline: your book -2.0% in the hour, by the record"
        assert "Your book moved -$200 (-2.0%)" in msg["body"]
        assert await bookmacro.send_book(eng, a.to_dict()) == 0  # once
        eng.desk.apply(DEV, "prefs", {"bookmacro": False})
        b = news("Tariffs on Canada doubled", tickers=())
        b.analysis.themes = ["trade_war"]
        eng.storage.add_alert(b)
        assert await bookmacro.send_book(eng, b.to_dict()) == 0  # switched off
    finally:
        await eng.http.close()


async def test_run54_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    voted(eng, 8, 7.0, 3.0)
    eng.refresh_base_rates()
    trade_war(eng)
    eng.desk.apply(DEV, "hold_set", {"symbol": "ACME", "shares": 10, "cost": 90})
    a = news("Tariffs on chips", tickers=())
    a.analysis.themes = ["trade_war"]
    eng.storage.add_alert(a)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/rvol"), headers=h) as r:
                assert (await r.json())["rows"][0]["band"] == "heavy"
            async with s.get(srv.make_url(f"/api/book-theme/{a.id}"), headers=h) as r:
                assert (await r.json())["estimate"]["rows"][0]["etf"] == "SMH"
    finally:
        await srv.close()
        await eng.http.close()
