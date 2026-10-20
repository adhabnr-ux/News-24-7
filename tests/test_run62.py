"""Run 62: every pick checked out of sample (the older two-thirds choose, the newest third grades,
and a pick that didn't hold falls back to the default) and the playbook's promises against its
results (each autopilot graded against the plain way on the same alerts)."""

from __future__ import annotations

import time

import aiohttp
from aiohttp.test_utils import TestServer

from news247.exits_kind import exit_rates
from news247.forward import forward, promise
from news247.holdout import check, stamp, use, words
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run16 import contract_history
from .test_run60 import rec

NAMES = {"close": "At the close", "d1": "The next day", "h1": "An hour in"}

# --------------------------------------------------------------------------- picks checked out of sample


def dated(**styles):
    """{style: [(t, x), ...]} from {style: [x, ...]}, oldest first."""
    return {s: [(float(i), x) for i, x in enumerate(xs)] for s, xs in styles.items()}


def test_a_pick_that_held():
    d = dated(close=[0.5] * 12, d1=[1.5] * 12)
    h = check(d, "close", "d1")
    assert h["pick"] == "d1" and h["held"] is True and h["n"] == 4 and h["avg"] == 1.5 and h["base"] == 0.5
    assert words(h, NAMES, "close") == "checked on the newest third: held (+1.5% vs +0.5% at the close, 4)"


def test_a_pick_that_was_luck():
    # the next day looked best overall only thanks to the older alerts; on the newest third it trailed
    d = dated(close=[0.2] * 8 + [0.6] * 4, d1=[2.5] * 8 + [-0.5] * 4)
    h = check(d, "close", "d1")
    assert h["pick"] == "d1" and h["held"] is False
    assert (
        words(h, NAMES, "close")
        == "on the newest third it made -0.5% vs +0.6% at the close (4): didn't hold, so at the close"
    )
    r = {"best": "d1", "label": "Contract win", "n": 12, "holdout": h,
         "styles": {"d1": {"avg": 1.5, "n": 12}, "close": {"avg": 0.33, "n": 12}}}  # fmt: skip
    assert use(r, "close") == "close"
    s = stamp(r, "contract", "close", NAMES)
    assert (
        s["best"] == "close"
        and s["pick"] == "d1"
        and s["held"] is False
        and set(s["styles"]) == {"d1", "close"}
    )


def test_an_unstable_pick_and_a_thin_one():
    # the older alerts picked an hour in; the full record picks the next day: not stable
    d = dated(close=[0.2] * 12, h1=[1.0] * 8 + [0.0] * 4, d1=[0.9] * 8 + [3.0] * 4)
    h = check(d, "close", "d1")
    assert h["pick"] == "h1" and h["held"] is False
    assert (
        words(h, NAMES, "close") == "the older alerts picked an hour in instead: not stable, so at the close"
    )
    thin = check(
        dated(close=[0.2] * 12, d1=[1.0] * 9), "close", "d1"
    )  # the next day only on the oldest alerts
    assert thin["held"] is None and use({"best": "d1", "holdout": thin}, "close") == "d1"
    assert check(dated(close=[0.1] * 6), "close", "close") is None  # too few to split
    assert check(dated(close=[0.1] * 12), "close", "close")["held"] is True  # the default needs no check


def test_the_rates_carry_the_check(tmp_path):
    eng = semis_engine(tmp_path)
    for i in range(12):
        a = news(f"Acme wins Pentagon contract {i}")
        a.created = time.time() - 86400 * (20 - i)  # i = 0 oldest
        a.edge = {
            "outcome": {"ACME": {"sign": 1, "close": 0.2 if i < 8 else 0.6, "d1": 2.5 if i < 8 else -0.5}}
        }
        eng.storage.add_alert(a)
    r = exit_rates(eng)["contract"]
    assert r["best"] == "d1" and r["holdout"]["held"] is False
    eng.exit_rates = {"contract": r}
    from news247.playbook import playbook

    rule = next(x for x in playbook(eng, "contract")["rules"] if x.startswith("Exit contract win news"))
    assert rule.endswith("didn't hold, so at the close.")


async def test_a_pick_that_didnt_hold_is_not_acted_on(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.refresh_base_rates()
    h = {"pick": "d1", "best": "d1", "since": 0, "n": 5, "avg": -0.5, "base": 0.6, "base_n": 5, "held": False}
    eng.exit_rates = {"contract": {"kind": "contract", "label": "Contract win", "n": 30, "best": "d1", "holdout": h,
                                   "styles": {"d1": {"n": 30, "avg": 1.2, "hit": 0.6}, "close": {"n": 30, "avg": 0.4, "hit": 0.55}}}}  # fmt: skip
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    xr = eng.storage.get_alert(a.id)["edge"]["exit_rec"]
    assert xr["best"] == "close" and xr["pick"] == "d1" and "didn't hold" in xr["check"]


# --------------------------------------------------------------------------- promises vs results


def test_the_promise_of_a_playbook_call():
    e = rec("alert", "d1")
    e["exit_rec"]["styles"] = {"d1": {"avg": 1.2}}
    assert promise(e)["promise"] == 1.2 and promise(e)["in"] == "alert" and promise(e)["out"] == "d1"
    e2 = rec("leg2", "close")
    e2["entry_rec"]["styles"] = {"leg2": {"avg": 0.9}}
    assert promise(e2)["promise"] == 0.9
    assert promise({}) is None


def closed_call(eng, i, live, close, rid="r1", direction="long", pb=None):
    a = news(f"Acme wins Pentagon contract {i}")
    a.created = time.time() - 86400 * (i + 2)
    a.edge = {"outcome": {"ACME": {"sign": 1, "close": close}}}
    eng.storage.add_alert(a)
    p = eng.desk.get(DEV)
    p.setdefault("closed", []).append({"id": f"c{i}", "symbol": "ACME", "direction": direction, "alert_id": a.id,
                                       "auto": rid, "pnl_pct": live, "closed": a.created + 3600, **({"pb": pb} if pb else {})})  # fmt: skip


def test_each_autopilot_against_the_plain_way(tmp_path):
    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    rid = eng.desk.get(DEV)["autopilots"][0]["id"]
    pb = {"kind": "contract", "label": "Contract win", "promise": 1.0}
    for i in range(10):
        closed_call(eng, i, 1.5 if i < 7 else -1.0, 0.5, rid=rid, pb=pb)  # live +0.75, plain +0.5
    closed_call(eng, 20, 2.0, -1.0, rid="gone", direction="short")  # short: the plain way made +1.0
    f = forward(eng, DEV)["rules"]
    s = f[rid]
    assert (
        s["n"] == 10
        and s["live"] == 0.75
        and s["plain"] == 0.5
        and s["edge"] == 0.25
        and s["verdict"] == "adds"
    )
    assert s["promised"] == 1.0 and s["promised_live"] == 0.75 and s["kinds"][0]["kind"] == "contract"
    assert s["line"] == (
        "Live +0.75% a call against +0.50% the plain way on the same alerts (10): its choices add +0.25% a call; "
        "the record promised +1.00%, live +0.75% (10)."
    )
    g = f["gone"]
    assert g["label"] == "A removed autopilot" and g["plain"] == 1.0 and "verdict" not in g


async def test_a_playbook_call_records_its_promise_and_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)
    eng.refresh_base_rates()
    eng.desk.apply(DEV, "autopilot_add", {"follow": "playbook", "min_sev": "MEDIUM"})
    eng.live_quotes.update("ACME", 100.5, 99.0, time.time() - 5, "tape")
    a = news("Acme wins $2B Pentagon contract")
    await eng.publish(a, push=False)
    e = rec("alert", "d1")
    e["exit_rec"]["styles"] = {"d1": {"avg": 1.2}}
    eng.storage.update_alert_edge(a.id, e)
    assert await eng.autopilot_open(eng.storage.get_alert(a.id)) == 1
    t = eng.desk.get(DEV)["tracked"][-1]
    assert t["pb"]["promise"] == 1.2 and t["pb"]["out"] == "d1"
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/autopilots/forward"), headers=h) as r:
                assert (await r.json()) == {"rules": {}, "need": 10}  # nothing closed yet
            async with s.get(srv.make_url("/api/exits-kind"), headers=h) as r:
                assert all("check" in row for row in (await r.json())["rows"])
    finally:
        await srv.close()
        await eng.http.close()
