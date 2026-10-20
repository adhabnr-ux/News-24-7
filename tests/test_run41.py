"""Run 41: into the close (hold each open call overnight or close it, from how alerts that stood the
same at the close did by the next one) and your edge by kind of news (your calls against the alerts
they came from)."""

from __future__ import annotations

import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247.myedge import my_edge
from news247.overnight import (
    calls_tonight,
    night,
    overnight_for,
    overnight_rates,
    read_of,
    send_overnight,
    standing,
)
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones

ET = ZoneInfo("America/New_York")
CLOSE = datetime(2026, 10, 14, 15, 45, tzinfo=ET).timestamp()  # Wednesday, 15:45 ET


def nights(eng, title, sym, close, d1, n=10, sign=1):
    for i in range(n):
        a = news(f"{title} number {i}", tickers=(sym,), direction="up" if sign > 0 else "down")
        a.created = CLOSE - 86400 * 10 - i * 600
        a.edge = {
            "refs": {sym: 100.0},
            "outcome": {sym: {"close": close * sign, "d1": d1 * sign, "sign": sign}},
        }
        eng.storage.add_alert(a)


# --------------------------------------------------------------------------- into the close


def test_the_night_after_the_close():
    assert night({"close": 2.0, "d1": 3.0, "sign": 1}) == (2.0, 0.98)
    assert night({"close": -2.0, "d1": -3.5, "sign": -1}) == (2.0, 1.53)  # a short, its way
    assert night({"close": 2.0, "sign": 1}) is None
    assert [standing(x) for x in (-1.5, -0.5, 0.9, 1.0, 2.9, 3.0)] == [
        "against",
        "flat",
        "flat",
        "up",
        "up",
        "big",
    ]
    assert read_of({"hit": 0.6, "median": 0.3}) == "hold" and read_of({"hit": 0.4, "median": 0.1}) == "close"
    assert (
        read_of({"hit": 0.6, "median": -0.1}) == "close" and read_of({"hit": 0.5, "median": 0.1}) == "either"
    )
    assert read_of(None) == "no record"


def test_the_overnight_record(tmp_path):
    eng = semis_engine(tmp_path)
    nights(eng, "Acme wins contract", "ACME", 2.0, 2.5)  # up 2% by the close, kept going
    nights(eng, "Beta downgraded to sell at Citi", "BETA", 4.0, 2.0, sign=-1)  # a big short, gave back
    rates = overnight_rates(eng, now=CLOSE)
    up = rates["contract|up"]
    assert up["n"] == 10 and up["hit"] == 1.0 and up["median"] == 0.49
    assert up["label"] == "Contract win up 1-3% its way at the close"
    assert rates["downgrade|big"]["hit"] == 0.0 and rates["downgrade|big"]["median"] == -2.08
    assert overnight_for(rates, "contract", 1.8)["key"] == "contract|up"
    assert overnight_for(rates, "fda_approval", 1.8)["key"] == "*|up"  # any news at that standing
    assert overnight_for(rates, "contract", -2.0) is None


def book(eng):
    nights(eng, "Acme wins contract", "ACME", 2.0, 2.5)
    eng.overnight_rates = overnight_rates(eng, now=CLOSE)
    a = news("Acme wins $900M Navy contract")
    a.created = CLOSE - 4 * 3600
    a.edge = {"refs": {"ACME": 100.0}}
    eng.storage.add_alert(a)
    eng.live_quotes.update("ACME", 102.0, 100.0, CLOSE - 60, "tape")
    eng.live_quotes.update("BETA", 50.0, 50.0, CLOSE - 60, "tape")
    d = eng.desk
    d.apply(
        DEV,
        "track_add",
        {"symbol": "ACME", "direction": "long", "entry": 100.5, "alert_id": a.id},
        lambda s: None,
    )
    d.apply(DEV, "track_add", {"symbol": "BETA", "direction": "long", "entry": 51.0}, lambda s: None)
    eng.trends = SimpleNamespace(earnings=SimpleNamespace(
        lookup=lambda s, t=None: {"date": "2026-10-15", "in_days": 1, "when": "before the open"} if s == "BETA" else None))  # fmt: skip
    return a


def test_each_open_call_gets_a_read(tmp_path):
    eng = semis_engine(tmp_path)
    book(eng)
    rows = {r["symbol"]: r for r in calls_tonight(eng, DEV, CLOSE)["rows"]}
    acme = rows["ACME"]
    assert (acme["pnl"], acme["its"], acme["standing"], acme["read"]) == (1.49, 2.0, "up", "hold")
    assert acme["record"]["key"] == "contract|up" and acme["report"] is None
    beta = rows["BETA"]  # no alert: its own P&L (-1.96%) picks the standing, any news
    assert (beta["standing"], beta["read"], beta["report"]) == ("against", "no record", "tomorrow")


async def test_the_push_into_the_close(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        book(eng)
        assert await send_overnight(eng, CLOSE - 1800) == 0  # 15:15: too early
        assert await send_overnight(eng, CLOSE) == 1
        msg = ph.read(server.requests[0]["body"])
        assert msg["title"] == "🌙 Into the close: 2 open calls · 1 hold, 0 close"
        lines = msg["body"].split("\n")
        assert lines[0] == (
            "ACME +1.5%: hold · contract win up 1-3% its way at the close went on its way by the next close 100% (median +0.5%, 10)"
        )
        assert lines[1] == "BETA -2.0%: no record yet · ⚠ reports tomorrow: a gap can go through a stop"
        assert await send_overnight(eng, CLOSE + 300) == 0  # once a day
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- your edge by kind


def test_your_edge_by_kind(tmp_path):
    eng = semis_engine(tmp_path)
    p = eng.desk.get(DEV)
    closed = []
    now = time.time()
    for i, (title, sym, alert_close, entry, pnl, direction) in enumerate(
        [("Acme wins contract", "ACME", 1.0, 99.5, 3.0, "long")]
        * 4  # bought a dip, held well: beat the alert
        + [("Beta downgraded to sell at Citi", "BETA", 2.0, 99.0, 0.2, "short")] * 4  # shorted late: trailed
    ):
        sign = 1 if direction == "long" else -1
        a = news(f"{title} {i}", tickers=(sym,), direction="up" if sign > 0 else "down")
        a.created = now - 86400 * (i + 2)
        a.edge = {"refs": {sym: 100.0}, "outcome": {sym: {"close": alert_close * sign, "sign": sign}}}
        eng.storage.add_alert(a)
        closed.append({"id": f"c{i}", "symbol": sym, "direction": direction, "entry": entry, "exit": None,
                       "ts": a.created + 300, "closed": a.created + 3600, "pnl_pct": pnl, "alert_id": a.id})  # fmt: skip
    closed.append(
        {"id": "x", "symbol": "ACME", "direction": "long", "entry": 100.0, "closed": now, "pnl_pct": 1.0}
    )
    p["closed"] = closed
    r = my_edge(eng, DEV, now=now)
    k = {x["key"]: x for x in r["kinds"]}
    c = k["contract"]
    assert (c["n"], c["you"], c["alert"], c["gap"], c["entry"], c["verdict"]) == (
        4,
        3.0,
        1.0,
        2.0,
        0.5,
        "yours",
    )
    assert c["rest"] == 1.5 and c["beat"] == 1.0
    d = k["downgrade"]
    assert (d["gap"], d["entry"], d["verdict"]) == (-1.8, -1.01, "alerts")
    assert r["all"]["n"] == 8 and r["kinds"][0]["key"] == "contract"
    assert r["lines"] == [
        "On contract win news you beat the alerts by +2.0% a call (4)",
        "On analyst downgrade news you trail the alerts by -1.8% a call (4): just following them did better",
    ]
    assert my_edge(eng, "dev_nobody_0000")["all"] is None


async def test_run41_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    book(eng)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/overnight"), headers=h) as r:
                assert r.status == 200 and "rows" in await r.json()
            async with s.get(srv.make_url("/api/myedge"), headers=h) as r:
                assert (await r.json())["all"] is None
            async with s.get(srv.make_url("/api/myedge"), headers={"Authorization": "Bearer tok"}) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()
