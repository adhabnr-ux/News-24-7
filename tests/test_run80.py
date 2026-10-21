"""Run 80: the broker mirror (the playbook autopilot's calls as Alpaca *paper* orders, with what real
fills cost against the desk's prices) and early exits on the record (the stall, give-back and event
exits stamped on every alert they cut, taken by the track record, and graded against holding)."""

from __future__ import annotations

import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import news247.mirror as mirror
from news247.early import board, cut, early_rates, stamp_closes, stamp_stalls
from news247.mirror import MirrorError, costs, report, switch, sync
from news247.pbcurve import taken
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run60 import et
from .test_run64 import pair

# --------------------------------------------------------------------------- the broker mirror


def test_paper_only():
    assert mirror._url("/v2/orders") == "https://paper-api.alpaca.markets/v2/orders"
    for bad in ("https://api.alpaca.markets", "https://example.com"):
        old, mirror.PAPER = mirror.PAPER, bad
        try:
            with pytest.raises(MirrorError):
                mirror._url("/v2/orders")
        finally:
            mirror.PAPER = old


class FakeAlpaca:
    """Alpaca paper's account and orders: market orders accepted, then filled at the next read."""

    def __init__(self, server, fills):
        self.fills, self.orders = fills, {}
        server.on(
            "/v2/account",
            {
                "status": "ACTIVE",
                "equity": "100000",
                "cash": "100000",
                "buying_power": "200000",
                "currency": "USD",
            },
        )
        server.on("/v2/orders", self.place)
        for i in range(1, 6):
            server.on(f"/v2/orders/o{i}", self.read)

    async def place(self, request):
        body = await request.json()
        oid = f"o{len(self.orders) + 1}"
        self.orders[oid] = {**body, "id": oid}
        return web.json_response(
            {"id": oid, "status": "accepted", "filled_qty": "0", "filled_avg_price": None}
        )

    async def read(self, request):
        oid = request.path.rsplit("/", 1)[1]
        o = self.orders[oid]
        return web.json_response({"id": oid, "status": "filled", "filled_qty": o["qty"],
                                  "filled_avg_price": str(self.fills[o["side"]]), "filled_at": "2026-10-21T14:31:02Z"})  # fmt: skip


def keyed(tmp_path, server):
    eng = semis_engine(tmp_path)
    eng.cfg.options.alpaca_key, eng.cfg.options.alpaca_secret = "paper-key", "paper-secret"
    mirror.PAPER = server.url("")
    return eng


@pytest.fixture(autouse=True)
def _paper_host():
    old = mirror.PAPER
    yield
    mirror.PAPER = old


def autopilot_call(eng, entry=100.0, shares=10):
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    p = eng.desk.get(DEV)
    rule = p["autopilots"][-1]["id"]
    eng.desk.apply(
        DEV, "track_add", {"symbol": "ACME", "direction": "long", "entry": entry, "alert_id": "a1"}, None
    )
    t = p["tracked"][-1]
    t["auto"], t["shares"] = rule, shares
    return p, t


async def test_switching_it_on(tmp_path, server):
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        with pytest.raises(MirrorError, match="no Alpaca keys"):
            await switch(eng, DEV, True)
        eng = keyed(tmp_path / "b", server)
        await eng.http.start()
        FakeAlpaca(server, {})
        r = await switch(eng, DEV, True)
        assert r["on"] and r["account"]["equity"] == "100000" and mirror.device(eng) == DEV
        acct = next(x for x in server.requests if x["path"] == "/v2/account")
        assert acct["headers"]["APCA-API-KEY-ID"] == "paper-key"
        assert (await switch(eng, DEV, False)) == {"on": False} and mirror.device(eng) == ""
    finally:
        await eng.http.close()


async def test_a_call_in_and_out_with_its_fills(tmp_path, server):
    eng = keyed(tmp_path, server)
    await eng.http.start()
    try:
        FakeAlpaca(server, {"buy": 100.2, "sell": 101.8})
        await switch(eng, DEV, True)
        p, t = autopilot_call(eng)
        assert await sync(eng) == 1
        order = next(x for x in server.requests if x["path"] == "/v2/orders")["json"]
        assert order == {"symbol": "ACME", "qty": "10", "side": "buy", "type": "market", "time_in_force": "day",
                         "client_order_id": f"ft-in-{t['id']}"}  # fmt: skip
        assert (
            await sync(eng) == 0 and mirror.log(eng)[t["id"]]["entry"]["fill"] == 100.2
        )  # the fill, read back
        eng.desk.apply(DEV, "track_close", {"id": t["id"]}, lambda s: (time.time(), 102.0, 2.0))
        assert await sync(eng) == 1  # out with it: the opposite order, the filled quantity
        out = [x for x in server.requests if x["path"] == "/v2/orders"][-1]["json"]
        assert out["side"] == "sell" and out["qty"] == "10" and out["client_order_id"] == f"ft-out-{t['id']}"
        await sync(eng)
        r = report(eng)
        assert r["n"] == 1 and r["entry"] == 0.2 and r["exit"] == 0.196 and r["round"] == 0.396
        assert r["desk"] == 2.0 and r["broker"] == 1.6
        assert r["line"] == ("On 1 mirrored call, real paper fills cost +0.40% a round trip against the desk's prices "
                             "(+0.20% in, +0.20% out): the broker made +1.60% a call where the desk's record says +2.00%.")  # fmt: skip
    finally:
        await eng.http.close()


async def test_only_fresh_playbook_calls(tmp_path, server):
    eng = keyed(tmp_path, server)
    await eng.http.start()
    try:
        FakeAlpaca(server, {"buy": 100.0})
        await switch(eng, DEV, True)
        p, t = autopilot_call(eng)
        t["ts"] = time.time() - 3600  # opened before the mirror was on: not sent
        mine = {"symbol": "BETA", "direction": "long", "entry": 50.0}
        eng.desk.apply(DEV, "track_add", mine, None)  # your own call, not the autopilot's: not sent
        assert await sync(eng) == 0
    finally:
        await eng.http.close()


async def test_a_rejected_entry_has_nothing_to_close(tmp_path, server):
    eng = keyed(tmp_path, server)
    await eng.http.start()
    try:
        FakeAlpaca(server, {})
        server.on("/v2/orders", (403, '{"message":"insufficient buying power"}'))
        await switch(eng, DEV, True)
        p, t = autopilot_call(eng)
        assert await sync(eng) == 1
        row = mirror.log(eng)[t["id"]]
        assert row["entry"]["status"] == "error" and "403" in row["entry"]["why"]
        eng.desk.apply(DEV, "track_close", {"id": t["id"]}, lambda s: (time.time(), 101.0, 1.0))
        await sync(eng)
        assert mirror.log(eng)[t["id"]]["exit"] == {"status": "no position"}
    finally:
        await eng.http.close()


def test_costs_for_a_short():
    row = {"direction": "short", "desk_entry": 50.0, "desk_exit": 48.0, "desk_pnl": 4.0,
           "entry": {"fill": 49.9}, "exit": {"fill": 48.1}}  # fmt: skip
    c = costs(row)
    assert c["entry"] == 0.2 and c["exit"] == 0.208 and c["broker"] == 3.61 and c["desk"] == 4.0


async def test_the_mirror_endpoints(tmp_path, server):
    eng = keyed(tmp_path, server)
    FakeAlpaca(server, {})
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            h = {"Authorization": "Bearer tok", "X-Device": DEV}
            async with s.post(srv.make_url("/api/mirror"), headers=h, json={"on": True}) as r:
                assert (await r.json())["on"] is True
            async with s.get(srv.make_url("/api/mirror"), headers=h) as r:
                body = await r.json()
                assert (
                    body["on"]
                    and body["yours"]
                    and body["keys"]
                    and body["account"]["buying_power"] == "200000"
                )
    finally:
        await srv.close()
        await eng.http.close()


# --------------------------------------------------------------------------- early exits


def a_graded(pout="d1", row=None, **edge):
    return {"id": "a1", "created": et(14, 11), "tickers": ["ACME"], "analysis": {"direction": "up"},
            "edge": {**pair("alert", pout), "refs": {"ACME": 100.0},
                     "outcome": {"ACME": row or {"sign": 1, "h1": 0.5, "close": 1.0, "d1": 3.0}}, **edge}}  # fmt: skip


def test_the_cut():
    early = {"rule": "giveback", "at": et(14, 15, 52), "move": 0.8}
    a = a_graded(early=early)
    assert cut(a) == 0.8 and taken(a) == (0.8, 1.0)  # out at the close's price, not tomorrow's +3%
    assert taken(a_graded(early={**early, "muted": True})) == (3.0, 1.0)  # a muted rule: held
    halves = a_graded("h1+d1", early=early)
    assert cut(halves) == 0.65  # the first half was out an hour in (+0.5%), the second cut at +0.8%


def test_stalls_are_stamped(tmp_path):
    eng = semis_engine(tmp_path)
    eng.stall_rates = {"*": {"stalled": {"n": 12, "avg": -0.6, "hit": 0.3}}}
    now = time.time()
    a = news("Acme wins Pentagon contract")
    a.created = now - 2100
    a.edge = {**pair("alert", "close"), "refs": {"ACME": 100.0}}
    eng.storage.add_alert(a)
    eng.live_quotes.update("ACME", 100.1, 99.0, now - 5, "tape")
    assert stamp_stalls(eng, now) == 1
    e = eng.storage.get_alert(a.id)["edge"]["early"]
    assert (
        e["rule"] == "stall" and e["move"] == 0.1 and not e["muted"] and "the time stop says out" in e["line"]
    )
    assert stamp_stalls(eng, now) == 0  # once


def test_the_event_exit_is_stamped_before_the_close(tmp_path):
    eng = semis_engine(tmp_path)
    a = news("Acme wins Pentagon contract")
    a.created = et(14, 11)
    a.edge = {**pair("alert", "d1"), "refs": {"ACME": 100.0}, "event_night": {"line": "ACME reports tonight"}}
    eng.storage.add_alert(a)
    now = et(14, 15, 52)
    eng.live_quotes.update("ACME", 101.0, 99.0, now - 5, "tape")
    assert stamp_closes(eng, now) == 1
    e = eng.storage.get_alert(a.id)["edge"]["early"]
    assert e["rule"] == "event" and e["move"] == 1.0
    assert stamp_closes(eng, et(14, 15, 30)) == 0  # not yet: only in the last 10 minutes


def test_a_rule_whose_cuts_cost_is_muted(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(24):  # the give-back exit kept cutting calls that went on to +3% the next day
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (30 - i)
        a.edge = {**pair("alert", "d1"), "refs": {"ACME": 100.0},
                  "outcome": {"ACME": {"sign": 1, "close": 1.0, "d1": 3.0}},
                  "early": {"rule": "giveback", "at": a.created + 3 * 3600, "move": 0.5}}  # fmt: skip
        eng.storage.add_alert(a)
    r = early_rates(eng)
    g = r["giveback"]
    assert (
        g["verdict"] == "costs"
        and g["cut"] == 0.5
        and g["hold"] == 3.0
        and g["added"] == -2.5
        and r["muted"] == ["giveback"]
    )
    assert board(r)[0]["line"] == (
        "Out at the close: the day gave back its run: on the 24 alerts it cut, the cut made +0.50% on average against "
        "+3.00% holding to the pair's own exit, worse in both halves (-2.50%, then -2.50% a cut): it's muted."
    )


async def test_a_muted_stall_rule_closes_nothing(tmp_path):
    eng = semis_engine(tmp_path)
    eng.early_rates = {"muted": ["stall"]}
    assert await eng.autopilot_stall() == 0


async def test_the_rules_board_has_the_exits(tmp_path):
    eng = semis_engine(tmp_path)
    eng.early_rates = {"stall": {"rule": "stall", "label": "The stall rule (a time stop 30 minutes in)", "n": 5, "cut": 0.1,
                                 "hold": -0.4, "added": 0.5, "older": 0.5, "newer": 0.5, "verdict": "earns"}, "muted": []}  # fmt: skip
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with (
            aiohttp.ClientSession() as s,
            s.get(srv.make_url("/api/playbook-rules"), headers={"Authorization": "Bearer tok"}) as r,
        ):
            body = await r.json()
            assert body["exits"][0]["line"].endswith("it adds +0.50% a cut.") and body["exits_muted"] == []
    finally:
        await srv.close()
        await eng.http.close()
