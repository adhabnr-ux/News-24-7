"""Run 20: linked devices (one desk on several devices) and today's setups (the best alerts since
the last close, as plans)."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.desk import Desk
from news247.plans import setups_push, todays_setups
from news247.storage import Storage
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import semis_engine
from .test_run3 import phones
from .test_run16 import contract_history, stored

ET = ZoneInfo("America/New_York")


# --------------------------------------------------------------------------- linked devices


def test_a_link_code_puts_a_second_device_on_the_desk():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA"]})
    d.apply(DEV2, "watch_add", {"symbol": "GAMA"})
    code = d.apply(DEV, "link_code", {})["link"]["code"]
    assert d.view(DEV, lambda s: None)["link"]["code"] == code
    assert d.use_link(code.lower()[:4] + "-" + code[4:], DEV2, bring=True) == DEV
    assert d.get(DEV)["watchlist"] == ["ACME", "BETA", "GAMA"]  # the joining device's stocks came along
    assert d.view(DEV, lambda s: None)["link"] is None  # used up
    with pytest.raises(ValueError, match="wrong or expired"):
        d.use_link(code, DEV2)  # one use
    code = d.apply(DEV, "link_code", {})["link"]["code"]
    with pytest.raises(ValueError, match="already uses"):
        d.use_link(code, DEV)
    code = d.apply(DEV, "link_code", {})["link"]["code"]
    assert d.use_link(code, "dev_third_device1") == DEV and d.get(DEV)["watchlist"] == [
        "ACME",
        "BETA",
        "GAMA",
    ]


def test_guessing_link_codes_is_stopped():
    d = Desk(Storage(":memory:"))
    code = d.new_link(DEV)
    for i in range(20):
        with pytest.raises(ValueError, match="wrong"):
            d.use_link(f"WRONG{i:03d}", DEV2)
    with pytest.raises(ValueError, match="too many wrong codes"):
        d.use_link(code, DEV2)  # even the right one, until the window passes
    assert d.use_link(code, DEV2, now=time.time() + 601) == DEV


async def test_linked_devices_get_every_push_and_can_be_listed_and_removed(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        a, b = phones(eng, server)  # phone b starts on its own desk (DEV2)
        h1 = {"Authorization": "Bearer tok", "X-Device": DEV}
        h2 = {"Authorization": "Bearer tok", "X-Device": DEV2}
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        async with aiohttp.ClientSession() as s:
            async with s.post(srv.make_url("/api/desk"), json={"action": "link_code"}, headers=h1) as r:
                code = (await r.json())["link"]["code"]
            async with s.post(srv.make_url("/api/link"), json={"code": "NOPE1234"}, headers=h2) as r:
                assert r.status == 400
            async with s.post(srv.make_url("/api/link"), json={"code": code}, headers=h2) as r:
                body = await r.json()
                assert body == {"device": DEV, "watchlist": 1}
            # the joining device re-subscribes under the desk's id (what the app does after linking)
            sub_b = next(x for x in eng.webpush.subs.values() if x.device == DEV2)
            sub_b.device = DEV
            await eng._to_device(DEV, {"id": "x", "title": "🔔 ACME is above $105.00"})
            assert sorted(r["path"] for r in server.requests) == ["/push/a", "/push/b"]  # both devices
            async with s.get(
                srv.make_url("/api/devices"), params={"endpoint": sub_b.endpoint}, headers=h1
            ) as r:
                devs = (await r.json())["devices"]
                assert len(devs) == 2 and [d["this"] for d in devs].count(True) == 1
                other = next(d for d in devs if d["this"])
            async with s.post(srv.make_url("/api/devices/remove"), json={"id": other["id"]}, headers=h1) as r:
                assert (await r.json())["removed"] is True
            async with s.post(srv.make_url("/api/devices/remove"), json={"id": other["id"]}, headers=h1) as r:
                assert r.status == 404
            async with s.get(srv.make_url("/api/devices"), headers=h1) as r:
                assert len((await r.json())["devices"]) == 1
    finally:
        await srv.close()
        await eng.http.close()


# --------------------------------------------------------------------------- today's setups


def test_todays_setups_are_ranked_plans(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)  # contract wins at $2-10B: 83% its way, typical 2.75%
    now = time.time()
    eng.live_quotes.update("ACME", 100.5, 99.0, now - 5, "tape")
    eng.live_quotes.update("BETA", 100.6, 99.0, now - 5, "tape")  # R:R 1.35, under ACME's 1.51
    a = stored(eng)  # ACME: R:R 1.51
    b = news("Beta wins Army contract", tickers=("BETA",))
    b.edge = {"refs": {"BETA": 100.0}, "pre": {"BETA": 1.0}}
    eng.storage.add_alert(b)  # BETA ($20B): its band is thin, so the any-size record (6) applies
    flat = stored(eng, "Acme hosts investor day")  # no kind of news: not a setup
    s = todays_setups(eng, DEV, now)
    assert [r["symbol"] for r in s["rows"]] == ["ACME", "BETA"]  # the better reward-to-risk first
    top = s["rows"][0]
    assert (
        top["catalyst"] == "Contract win"
        and top["record"]["n"] == 6
        and top["plan"]["rr"] == pytest.approx(1.51, abs=0.01)
    )
    assert s["rows"][1]["plan"]["rr"] == pytest.approx(1.35, abs=0.01)
    eng.desk.apply(DEV, "watch_add", {"symbol": "BETA"})
    mine = todays_setups(eng, DEV, now)["rows"]
    assert mine[0]["symbol"] == "BETA" and mine[0]["mine"]  # your stock a step ahead
    msg = setups_push(todays_setups(eng, DEV, now))
    assert msg["title"] == "🎯 2 setups for today" and msg["body"].startswith("★ BETA long · R:R 1.4")
    assert setups_push({"rows": []}) is None
    assert flat["id"] and a["id"]


async def test_setups_push_at_925_to_phones_that_ask(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    contract_history(eng)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        at = datetime(2026, 10, 14, 9, 26, tzinfo=ET).timestamp()
        eng.live_quotes.update("ACME", 100.5, 99.0, at - 5, "tape")
        x = stored(eng)
        eng.storage.db.execute("UPDATE alerts SET created = ? WHERE id = ?", (at - 3600, x["id"]))
        assert await eng.send_setups(at) == 0  # nobody asked
        eng.desk.apply(DEV, "prefs", {"setups": True})
        assert await eng.send_setups(at - 120) == 0  # 09:24: not yet
        assert await eng.send_setups(at) == 1 and await eng.send_setups(at + 60) == 0  # once a day
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "🎯 1 setup for today" and msg["open"] == "/app/#a=brief"
    finally:
        await eng.http.close()


async def test_run20_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    stored(eng)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/setups"), headers=h) as r:
                assert (await r.json())["rows"][0]["symbol"] == "ACME"
            async with s.get(srv.make_url("/api/devices"), headers={"Authorization": "Bearer tok"}) as r:
                assert r.status == 400
    finally:
        await srv.close()
        await eng.http.close()
