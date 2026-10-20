"""Run 22: needs you (one ranked list of what wants a decision now) and regime-aware records (each
kind of news graded separately on risk-on, risk-off and mixed days)."""

from __future__ import annotations

import json
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.actions import action_center
from news247.catalysts import catalyst_table, regime_base, regime_rates
from news247.models import Severity
from news247.web.server import WebServer

from .test_desk import DEV, DEV2, news
from .test_personal import semis_engine
from .test_run15 import graded
from .test_run16 import contract_history, stored

ET = ZoneInfo("America/New_York")


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).date().isoformat()


def remember(eng, ts: float, regime: str) -> None:
    eng.storage.set_setting(f"memory:{day_of(ts)}", json.dumps({"regime": regime}))


def regime_history(eng):
    """Contract wins: six on a risk-off day (83% their way), four on a risk-on day (25%), three on a
    mixed day (too few for their own record)."""
    now = time.time()
    remember(eng, now - 86400 * 3, "risk-off")
    for i, r in enumerate([3.0, 2.0, 4.0, -1.0, 2.5, 3.5]):
        eng.storage.add_alert(graded(f"Acme wins contract number {i}", r))  # 3 days ago
    remember(eng, now - 86400 * 5, "risk-on")
    for i, r in enumerate([-1.0, -2.0, 1.0, -0.5]):
        a = graded(f"Acme wins contract award {i}", r)
        a.created = now - 86400 * 5
        eng.storage.add_alert(a)
    remember(eng, now - 86400 * 7, "mixed")
    for i, r in enumerate([1.0, 2.0, 3.0]):
        a = graded(f"Acme wins contract deal {i}", r)
        a.created = now - 86400 * 7
        eng.storage.add_alert(a)
    a = graded("Acme wins contract with no memory", 9.0)  # a day market memory never saw: left out
    a.created = now - 86400 * 9
    eng.storage.add_alert(a)


# --------------------------------------------------------------------------- regime-aware records


def test_kinds_of_news_are_graded_by_the_regime_of_their_day(tmp_path):
    eng = semis_engine(tmp_path)
    regime_history(eng)
    rates = regime_rates(eng)
    assert rates["contract|risk-off"] == {"n": 6, "hit": 0.83, "median": 2.75}
    assert rates["contract|risk-on"] == {"n": 4, "hit": 0.25, "median": -0.75}
    assert rates["contract|mixed"]["n"] == 3 and sum(r["n"] for r in rates.values()) == 13  # the 9.0 isn't in
    assert regime_base(rates, "contract", "risk-off") == {
        "n": 6,
        "hit": 0.83,
        "median": 2.75,
        "regime": "risk-off",
    }
    assert regime_base(rates, "contract", "risk-on")["n"] == 4  # four is enough
    assert regime_base(rates, "contract", "mixed") is None  # three is not
    assert regime_base(rates, "contract", "unknown") is None and regime_base(rates, None, "risk-off") is None
    eng.refresh_base_rates()
    table = catalyst_table(eng.catalyst_rates, regimes=eng.regime_rates)
    assert [(g["regime"], g["n"]) for g in table[0]["regimes"]] == [
        ("risk-on", 4),
        ("mixed", 3),
        ("risk-off", 6),
    ]
    assert catalyst_table(eng.catalyst_rates)[0]["regimes"] == []


async def test_a_new_alert_carries_its_record_on_days_like_today(tmp_path):
    eng = semis_engine(tmp_path)
    regime_history(eng)
    eng.refresh_base_rates()
    now = time.time()
    eng._regime_now = (now, "risk-off")  # the live read: risk-off today
    a = news("Acme wins $900M Navy contract")
    await eng.publish(a, push=False)
    edge = eng.storage.get_alert(a.id)["edge"]
    assert edge["regime_base"] == {"n": 6, "hit": 0.83, "median": 2.75, "regime": "risk-off"}
    body = eng.webpush.payload(a)["body"]
    assert "Contract win, $2-10B:" in body and body.endswith("· on risk-off days: 83% (6)")
    eng._regime_now = (now, "mixed")  # too few graded on mixed days: no line
    b = news("Acme wins $1B Army contract")
    await eng.publish(b, push=False)
    assert "regime_base" not in eng.storage.get_alert(b.id)["edge"]
    assert "days:" not in eng.webpush.payload(b)["body"]


def test_the_live_regime_is_read_at_most_every_five_minutes(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    eng._regime_now = (now, "risk-on")
    assert eng.regime_label(now + 60) == "risk-on"  # cached
    assert eng.regime_label(now + 400) == "unknown"  # re-read: no quotes yet, so no read
    assert eng._regime_now == (now + 400, "unknown")


# --------------------------------------------------------------------------- needs you


def desk_with_everything(eng, now):
    q = eng.live_quotes
    q.update("ACME", 99.5, 100.0, now - 5, "tape")
    q.update("BETA", 94.5, 100.0, now - 5, "tape")
    q.update("GAMA", 104.2, 100.0, now - 5, "tape")
    q.update("DLTA", 100.0, 100.0, now - 5, "tape")
    d = eng.desk
    d.apply(DEV, "watch_add", {"symbols": ["DLTA"]})
    d.apply(DEV, "hold_set", {"symbol": "ACME", "shares": 40})
    d.apply(
        DEV, "track_add", {"symbol": "ACME", "direction": "long", "entry": 100, "stop": 99, "target": 106}
    )
    d.apply(DEV, "track_add", {"symbol": "BETA", "direction": "long", "entry": 100, "stop": 95})
    assert len(d.due_levels(eng.price_of, now)) == 1  # BETA's stop: hit
    d.apply(DEV, "alert_add", {"symbol": "GAMA", "kind": "above", "value": 105})
    d.apply(DEV, "alert_add", {"symbol": "GAMA", "kind": "above", "value": 150})  # far: not near
    d.get(DEV)["reminders"] = [{"id": "r1", "alert_id": "x1", "title": "Acme FDA panel", "tickers": ["ACME"],
                                "due": now + 1800, "created": now}]  # fmt: skip
    nxt = {"ACME": ("today", 0, "after the close"), "DLTA": ("tomorrow", 1, "")}
    eng.trends = SimpleNamespace(earnings=SimpleNamespace(
        lookup=lambda s, t=None: {"date": nxt[s][0], "in_days": nxt[s][1], "when": nxt[s][2]} if s in nxt else None))  # fmt: skip
    eng.implied_cache["ACME"] = (now, {"rows": [{"expiry": "x", "move_pct": 6.0, "earnings": True}]})
    # today's alerts: one CRITICAL on DLTA, one HIGH on ACME, one opened, one not yours, one too small
    crit = news("Delta halted pending news", tickers=("DLTA",), sev=Severity.CRITICAL)
    high = news("Acme wins $2B Pentagon contract")
    seen = news("Acme raises guidance")
    other = news("Zeta wins contract", tickers=("ZETA",))
    low = news("Acme presents at a conference", sev=Severity.MEDIUM)
    for a in (crit, high, seen, other, low):
        eng.storage.add_alert(a)
    d.apply(DEV, "opened", {"id": seen.id})
    day = day_of(now)
    eng.storage.set_setting("diverge_log", json.dumps([
        {"ts": now - 60, "day": day, "alert_id": crit.id, "symbol": "DLTA", "sign": 1, "against": 2.1, "close": None},
        {"ts": now - 60, "day": "2020-01-02", "alert_id": high.id, "symbol": "ACME", "sign": 1, "against": 3.0, "close": None}]))  # fmt: skip
    eng.storage.set_setting("retest_log", json.dumps([
        {"ts": now - 90, "day": day, "alert_id": seen.id, "symbol": "ZETA", "sign": 1, "ran": 1.8, "close": None},
        {"ts": now - 90, "day": day, "alert_id": other.id, "symbol": "ZETA", "sign": 1, "ran": 1.2, "close": None}]))  # fmt: skip
    return crit, high, seen


def test_needs_you_ranks_everything_that_wants_a_decision(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    crit, high, seen = desk_with_everything(eng, now)
    r = action_center(eng, DEV, now)
    kinds = [i["kind"] for i in r["items"]]
    assert kinds == ["stop_hit", "report", "near_stop", "diverge", "unread", "reminder", "retest", "unread",
                     "near_alert", "report"]  # fmt: skip
    assert (r["count"], r["urgent"], r["hidden"]) == (10, 4, 0)
    it = {i["key"].split(":")[0] + ":" + i["symbol"]: i for i in r["items"]}
    stop = r["items"][0]
    assert stop["title"] == "BETA hit your stop ($95.00)" and stop["urgency"] == 95
    assert stop["detail"] == "long from $100.00 · now $94.50 (-5.50%). Close it, or move the stop."
    assert stop["do"]["action"] == "track_close" and stop["do"]["body"]["lesson"] == "stopped out"
    rep = it["earn:ACME"]
    assert rep["title"] == "ACME reports after the close today"
    assert rep["detail"] == "options price ±6.0% · about ±$239 on your 40 shares"  # 40 x $99.50 x 6%
    near = it["near_stop:ACME"]
    assert near["title"] == "ACME is 0.5% from your stop ($99.00)" and near["urgency"] == pytest.approx(
        77.5, abs=0.1
    )
    dv = it["dv:DLTA"]
    assert dv["title"] == "DLTA is going against its good news" and dv["open"] == f"#a={crit.id}"
    assert it["unread:DLTA"]["detail"].startswith("CRITICAL · 1 min ago")
    assert (
        it["rem:ACME"]["title"] == "Reminder in 30 min" and it["rem:ACME"]["do"]["action"] == "remind_clear"
    )
    assert it["rt:ZETA"]["open"] == f"#a={seen.id}"  # not your stock, but you opened its alert
    assert it["unread:ACME"]["open"] == f"#a={high.id}"  # the opened one and the MEDIUM one aren't listed
    assert it["pa:GAMA"]["title"] == "GAMA is 0.8% from your $105.00 alert"
    assert (
        it["earn:DLTA"]["title"] == "DLTA reports tomorrow"
        and it["earn:DLTA"]["detail"] == "on your watchlist"
    )
    assert action_center(eng, DEV2, now) == {"items": [], "count": 0, "urgent": 0, "hidden": 0}


def test_needs_you_items_are_settled_or_dismissed(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    desk_with_everything(eng, now)
    first = action_center(eng, DEV, now)["items"][0]
    eng.desk.apply(DEV, first["do"]["action"], first["do"]["body"], eng.price_of)  # close BETA from the item
    assert eng.desk.get(DEV)["closed"][0]["lesson"] == "stopped out"
    r = action_center(eng, DEV, now)
    assert r["count"] == 9 and r["items"][0]["kind"] == "report"
    eng.desk.apply(DEV, "act_done", {"key": r["items"][0]["key"]})
    r2 = action_center(eng, DEV, now)
    assert (r2["count"], r2["hidden"]) == (8, 1) and r2["items"][0]["kind"] == "near_stop"
    assert action_center(eng, DEV, now + 21 * 3600)["hidden"] == 0  # back after 20 hours
    eng.desk.apply(DEV, "act_reset", {})
    assert action_center(eng, DEV, now)["count"] == 9
    with pytest.raises(ValueError):
        eng.desk.apply(DEV, "act_done", {})


def test_needs_you_reads_the_autopilot_room_left_and_the_portfolio(tmp_path):
    eng = semis_engine(tmp_path)
    contract_history(eng)  # contract wins, $2-10B: typical +2.75% by the close
    now = time.time()
    eng.live_quotes.update("ACME", 100.5, 99.0, now - 5, "tape")
    eng.live_quotes.update("DLTA", 94.0, 100.0, now - 5, "tape")
    eng.live_quotes.update("GAMA", 105.0, 100.0, now - 5, "tape")
    a = stored(eng)  # ACME, from $100: +0.5% so far
    d = eng.desk
    d.apply(DEV, "watch_add", {"symbol": "ACME"})
    d.apply(DEV, "hold_set", {"symbol": "DLTA", "shares": 100})
    d.apply(DEV, "prefs", {"port_alert": 3})
    d.apply(DEV, "alert_add", {"symbol": "ACME", "kind": "move", "value": 2})
    d.apply(DEV, "alert_add", {"symbol": "GAMA", "kind": "trail", "value": 5})
    d.get(DEV)["price_alerts"][-1]["peak"] = 110.0  # fires under $104.50
    d.get(DEV)["tracked"].append({"id": "t1", "symbol": "ACME", "direction": "long", "entry": 100.0, "ts": now,
                                  "auto": "r1", "exit_at": now + 600, "stop": 99.0, "target": 103.0})  # fmt: skip
    r = action_center(eng, DEV, now)
    it = {i["kind"]: i for i in r["items"]}
    assert it["portfolio"]["title"] == "Your portfolio is -6.0% today"
    assert it["portfolio"]["detail"] == "−$600 · past your ±3% line"
    assert (
        it["big_move"]["title"] == "DLTA -6.0% today"
        and it["big_move"]["detail"] == "−$600 on your 100 shares"
    )
    assert it["room"]["title"] == "ACME has room left by its record" and it["room"]["open"] == f"#a={a['id']}"
    assert it["room"]["detail"].startswith("+0.5% its way so far, of a typical +2.8% (Contract win, 83%")
    assert it["auto_exit"]["title"] == "Autopilot closes long ACME in 10 min"
    assert it["auto_exit"]["detail"] == "now $100.50 (+0.50%)"
    pa = {i["title"] for i in r["items"] if i["kind"] == "near_alert"}
    assert pa == {
        "ACME is +1.5% today: 0.5 pts from your ±2% alert",
        "GAMA is 0.5% above its trailing stop ($104.50)",
    }
    assert not any(
        i["kind"].startswith("near_") and i["symbol"] == "ACME" and "stop" in i["kind"] for i in r["items"]
    )
    assert [i["kind"] for i in r["items"]][:2] == ["portfolio", "big_move"]


async def test_run22_over_the_api(tmp_path):
    eng = semis_engine(tmp_path)
    regime_history(eng)
    eng.refresh_base_rates()
    eng._regime_now = (time.time(), "risk-off")
    now = time.time()
    desk_with_everything(eng, now)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(srv.make_url("/api/actions"), headers=h) as r:
                body = await r.json()
                assert body["count"] == 10 and body["items"][0]["kind"] == "stop_hit"
            async with s.get(srv.make_url("/api/actions"), headers={"Authorization": "Bearer tok"}) as r:
                assert r.status == 400
            async with s.post(
                srv.make_url("/api/desk"),
                json={"action": "act_done", "key": body["items"][0]["key"]},
                headers=h,
            ) as r:
                assert r.status == 200
            async with s.get(srv.make_url("/api/actions"), headers=h) as r:
                assert (await r.json())["hidden"] == 1
            async with s.get(srv.make_url("/api/record"), headers=h) as r:
                rec = await r.json()
                assert rec["regime_now"] == "risk-off"
                assert {g["regime"] for g in rec["catalysts"][0]["regimes"]} == {
                    "risk-on",
                    "mixed",
                    "risk-off",
                }
    finally:
        await srv.close()
        await eng.http.close()
