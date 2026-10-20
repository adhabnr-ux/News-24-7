"""Run 38: insider buys graded by shape (who, how much, how unusual, with others, after a drop, on a
plan), and a month on (every alert graded 20 sessions out; which kinds of news kept drifting and
which gave it back, with a drift check five sessions after news on your stock)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp.test_utils import TestServer

from news247.dossier import record_month, session_closes
from news247.drift import drift_board, drift_for, drift_line, drift_rates, send_drift_checks, verdict
from news247.insiders import insider_base, insider_board, insider_line, insider_rates, read_buy, role_bucket
from news247.web.server import WebServer

from .test_desk import DEV, news
from .test_personal import semis_engine
from .test_run3 import phones

ET = ZoneInfo("America/New_York")


def et(month, day, hour, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=ET).timestamp()


NOW = et(10, 14, 11)  # Wednesday, October 14


# --------------------------------------------------------------------------- a month on


def test_every_alert_is_graded_a_month_on(tmp_path):
    eng = semis_engine(tmp_path)
    a = news("Acme wins contract")
    a.created = et(9, 2, 10)
    a.edge = {"refs": {"ACME": 100.0}, "mkt_ref": 600.0,
              "outcome": {"ACME": {"h1": 1.0, "close": 2.0, "d1": 2.5, "d5": 3.0, "sign": 1}},
              "mkt_outcome": {"close": 0.1}}  # fmt: skip
    eng.storage.add_alert(a)
    ts = session_closes(a.created, 21, eng.calendar.holidays)[20]
    assert datetime.fromtimestamp(ts, ET).date().isoformat() == "2026-10-01"  # 20 sessions (Labor Day off)
    eng.live_quotes.update("ACME", 108.0, 107.0, ts - 120, "tape")
    eng.live_quotes.update("SPY", 612.0, 611.0, ts - 120, "tape")
    assert record_month(eng, ts - 3600) == 0  # not yet
    assert record_month(eng, ts + 60) == 1
    e = eng.storage.get_alert(a.id)["edge"]
    assert e["outcome"]["ACME"]["d20"] == 8.0 and e["mkt_outcome"]["d20"] == 2.0
    assert e["mkt_outcome"]["close"] == 0.1  # the rest kept
    assert record_month(eng, ts + 120) == 0  # once


def graded(eng, title, sym, d5, d20, sign=1, n=10, extra=None, mkt=None):
    for i in range(n):
        a = news(
            f"{title} number {i}", tickers=(sym,), direction="up" if sign > 0 else "down", **(extra or {})
        )
        a.created = NOW - 86400 * 40 - i * 3600
        x5, x20 = (d5[i % len(d5)], d20[i % len(d20)]) if isinstance(d5, list) else (d5, d20)
        a.edge = {
            "refs": {sym: 100.0},
            "outcome": {sym: {"close": 1.0 * sign, "d5": x5 * sign, "d20": x20 * sign, "sign": sign}},
        }
        if mkt:
            a.edge["mkt_outcome"] = {"d5": mkt[0], "d20": mkt[1]}
        eng.storage.add_alert(a)


def test_which_kinds_kept_drifting(tmp_path):
    eng = semis_engine(tmp_path)
    graded(eng, "Acme wins contract", "ACME", 1.5, 4.2, mkt=(0.5, 1.0))
    graded(eng, "Beta downgraded to sell at Citi", "BETA", 2.0, 0.5, sign=-1)
    rates = drift_rates(eng, now=NOW)
    c, d = rates["contract"], rates["downgrade"]
    assert c["verdict"] == "drifts" and c["d20"] == {"n": 10, "hit": 1.0, "median": 4.2}
    assert c["x_d20"]["median"] == 3.2  # beyond SPY over the same month
    assert d["verdict"] == "gives_back"
    assert (
        drift_line(c)
        == "Contract win news here: +1.5% five sessions on, +4.2% a month on (median, its way, 10; +3.2% against SPY): it kept drifting for a month"
    )
    assert drift_line(d).endswith("it gave it back over the month")
    assert (
        verdict({"d5": {"n": 9, "hit": 0.6, "median": 1.0}, "d20": {"n": 9, "hit": 0.6, "median": 1.4}})
        == "holds"
    )
    assert (
        verdict({"d5": {"n": 9, "hit": 0.6, "median": 1.0}, "d20": {"n": 5, "hit": 1.0, "median": 9.0}})
        is None
    )
    board = drift_board(rates)
    assert board["rows"][0]["key"] == "contract" and board["rows"][-1]["key"] == "downgrade"
    assert drift_for(rates, news("Acme wins new contract").to_dict(), eng.universe)["key"] == "contract"


async def test_the_drift_check_five_sessions_on(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    graded(eng, "Acme wins contract", "ACME", 1.5, 4.2)
    eng.drift_rates = drift_rates(eng, now=NOW)
    a = news("Acme wins $900M Navy contract")
    a.created = et(10, 7, 10)  # five sessions before Wednesday the 14th
    a.edge = {"refs": {"ACME": 100.0}, "outcome": {"ACME": {"close": 1.0, "d1": 1.5, "d5": 2.4, "sign": 1}}}
    eng.storage.add_alert(a)
    await eng.http.start()
    try:
        ph, _ = phones(eng, server)
        eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
        assert await send_drift_checks(eng, et(10, 14, 15, 0)) == 0  # before the close
        assert await send_drift_checks(eng, et(10, 14, 16, 10)) == 1
        msg = ph.read(server.requests[0]["body"])
        assert msg["title"] == "📈 ACME, five sessions on: +2.4% its way"
        assert msg["body"].endswith(
            "Contract win news here: +1.5% five sessions on, +4.2% a month on (median, its way, 10): it kept drifting for a month"
        )
        assert await send_drift_checks(eng, et(10, 14, 16, 20)) == 0  # once
        assert await send_drift_checks(eng, et(10, 15, 16, 10)) == 0  # the next day: not its fifth session
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- insider buys


def buy(role="CEO", value=2_000_000, pattern="opportunistic", cluster=("Jane Roe",), off=-25.0, plan=False):
    return {"insider_buy": {"owner": "John Doe", "role": role, "value": value, "pattern": pattern, "cluster": list(cluster),
                            "off_3m_high_pct": off, "plan_10b5_1": plan}}  # fmt: skip


def test_reading_an_insider_buy():
    assert [role_bucket(r) for r in ("CEO", "President", "CFO", "Director", "10% owner", "Officer", "VP Sales", "Insider")] == [
        "top", "top", "cfo", "director", "owner", "officer", "officer", "other"]  # fmt: skip
    s = read_buy(news("Acme CEO buys $2M of stock", **buy()).to_dict())
    assert {k: s[k] for k in ("role", "size", "pattern", "cluster", "dip", "plan")} == {
        "role": "top", "size": "big", "pattern": "opportunistic", "cluster": True, "dip": True, "plan": False}  # fmt: skip
    assert s["label"] == "CEO-level, $1M+, opportunistic, a cluster, after a 20%+ drop"
    small = read_buy(news("x", **buy("Director", 50_000, "routine", (), -5.0, True)).to_dict())
    assert small["label"] == "Director, under $100K, routine, on a 10b5-1 plan"
    assert read_buy(news("Acme wins contract").to_dict()) is None


def insider_history(eng):
    graded(eng, "Acme CEO buys stock", "ACME", [3.0, 4.0, 2.0, -1.0, 5.0], [6.0, 8.0, 1.0, -2.0, 9.0], extra=buy(),
           mkt=(0.5, 1.0))  # fmt: skip
    graded(eng, "Beta director buys stock", "BETA", [-0.5, 0.2, -1.0, -0.3], [-1.0, 0.5, -2.0, 0.0],
           extra=buy("Director", 50_000, "routine", (), -5.0, True))  # fmt: skip


def test_insider_buys_are_graded_by_shape(tmp_path):
    eng = semis_engine(tmp_path)
    insider_history(eng)
    rates = insider_rates(eng, now=NOW)
    assert rates["*"]["d5"]["n"] == 20
    top = rates["role+pattern:top+opportunistic"]
    assert top["label"] == "CEO-level, opportunistic" and top["d5"] == {"n": 10, "hit": 0.8, "median": 3.0}
    assert top["d20"]["median"] == 6.0 and top["x_d20"]["median"] == 5.0
    assert rates["plan:yes"]["d5"]["hit"] == 0.3 and rates["cluster:yes"]["d5"]["n"] == 10
    b = insider_base(rates, read_buy(news("x", **buy()).to_dict()))
    assert b["key"] == "role+pattern:top+opportunistic"
    assert (
        insider_line(b)
        == "Insider buys like this (CEO-level, opportunistic): 80% up five sessions on (median +3.0%, 10), +6.0% a month on (10)"
    )
    # a CFO's buy: nothing that specific, so the record falls back to its pattern
    assert insider_base(rates, read_buy(news("x", **buy("CFO")).to_dict()))["key"] == "pattern:opportunistic"
    board = insider_board(rates)
    role = next(d for d in board["dims"] if d["key"] == "role")
    assert [r["label"] for r in role["rows"]] == ["CEO, president or chair", "Directors"]
    assert board["best"][0]["key"].startswith("role+") and board["all"]["d5"]["n"] == 20
    assert {r["label"] for r in board["worst"]} == {"Director, routine", "Director, under $100K"}


async def test_a_new_insider_buy_carries_its_record(tmp_path):
    eng = semis_engine(tmp_path)
    insider_history(eng)
    eng.refresh_base_rates()
    a = news("Acme CEO John Doe buys $3M of stock", **buy(value=3_000_000))
    await eng.publish(a, push=False)
    assert (
        a.edge["insider"]["role"] == "top"
        and a.edge["insider_base"]["key"] == "role+pattern:top+opportunistic"
    )
    assert (
        "Insider buys like this (CEO-level, opportunistic): 80% up five sessions on"
        in eng.webpush.payload(a)["body"]
    )


async def test_run38_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    insider_history(eng)
    graded(eng, "Acme wins contract", "ACME", 1.5, 4.2)
    eng.refresh_base_rates()
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/insiders"), headers=h) as r:
                assert (await r.json())["all"]["d5"]["n"] == 20
            async with s.get(srv.make_url("/api/drift"), headers=h) as r:
                assert "contract" in [x["key"] for x in (await r.json())["rows"]]
    finally:
        await srv.close()
        await eng.http.close()
