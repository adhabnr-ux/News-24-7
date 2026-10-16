"""Run 4 of the desk: options positioning, find-why on demand, digest mode, moving a desk to
another phone, earnings-day reminders, the open plan, notes and groups, 👍/👎 on alerts, and the
"historically fades" flag."""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from news247.desk import Desk
from news247.dossier import base_line, base_rate
from news247.market.options import Chain, Contract
from news247.personal import positioning
from news247.storage import Storage

from .test_desk import DEV, DEV2, news
from .test_personal import quotes, semis_engine
from .test_run3 import item, phones

ET = ZoneInfo("America/New_York")


def at(h, m, d=14):
    return datetime(2026, 10, d, h, m, tzinfo=ET).timestamp()  # Oct 14 2026 is a Wednesday


# --------------------------------------------------------------------------- options positioning


def test_positioning_walls_and_max_pain():
    today = date(2026, 10, 14)
    e1, e2 = today + timedelta(days=2), today + timedelta(days=9)
    legs = [
        Contract("C95", "ACME", e1, "C", 95, volume=100, open_interest=500),
        Contract("C100", "ACME", e1, "C", 100, volume=900, open_interest=2000),
        Contract("C110", "ACME", e1, "C", 110, volume=300, open_interest=9000),  # the call wall
        Contract("P90", "ACME", e1, "P", 90, volume=200, open_interest=7000),  # the put wall
        Contract("P100", "ACME", e1, "P", 100, volume=400, open_interest=1000),
        Contract("C120", "ACME", e2, "C", 120, volume=1000, open_interest=None),  # OI unknown: not zero
    ]
    p = positioning(Chain("ACME", price=101.0, contracts=legs), today)
    assert p["call_volume"] == 2300 and p["put_volume"] == 600 and p["pc_volume"] == 0.26
    assert p["expiry"] == e1.isoformat() and p["dte"] == 2
    assert p["call_wall"] == {"strike": 110, "oi": 9000} and p["put_wall"] == {"strike": 90, "oi": 7000}
    assert p["pc_oi"] == round(8000 / 11500, 2)

    # max pain: the strike where holders collect least, checked against a direct sum
    def paid(s):
        return sum(
            (c.open_interest or 0) * (max(0, s - c.strike) if c.cp == "C" else max(0, c.strike - s))
            for c in legs
            if c.expiry == e1
        )

    assert p["max_pain"] == min((90, 95, 100, 110), key=paid)
    no_oi = positioning(
        Chain("ACME", price=101.0, contracts=[Contract("C1", "ACME", e1, "C", 100, volume=5)]), today
    )
    assert no_oi == {"call_volume": 5, "put_volume": 0, "pc_volume": 0.0}
    assert positioning(Chain("ACME"), today) is None


# --------------------------------------------------------------------------- find why


async def test_find_why_on_demand(tmp_path):
    eng = semis_engine(tmp_path)
    eng.why = None
    assert (await eng.find_why("ACME"))["available"] is False
    calls = []

    async def hunt(sym, name, since):
        calls.append((sym, name))
        return [
            item("Acme 8-K: entry into a material definitive agreement", source="sec-8k"),
            item("Acme shares jump", source="gnews"),
        ]

    allowed = iter([True, False])
    eng.why = SimpleNamespace(allow=lambda s, now=None: next(allowed), hunt=hunt)
    r = await eng.find_why("ACME")
    assert r["available"] and calls == [("ACME", "Acme Semiconductor")] and len(r["found"]) == 2
    assert r["found"][0]["score"] >= r["found"][1]["score"]
    assert eng.storage.seen(item("Acme shares jump", source="gnews").uid)  # scored and stored like any item
    busy = await eng.find_why("ACME")
    assert busy["busy"] and busy["found"] == []


# --------------------------------------------------------------------------- digest mode


async def test_digest_holds_alerts_and_sends_one_push(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, b = phones(eng, server)
        with pytest.raises(ValueError, match="0, 15 or 60"):
            eng.desk.apply(DEV, "prefs", {"digest": 5})
        eng.desk.apply(DEV, "prefs", {"digest": 15})
        eng.desk.get(DEV)["digest_at"] = time.time()  # a digest just went
        await eng.dispatcher.dispatch(news("Acme wins $2B Pentagon contract"))
        await eng.dispatcher.dispatch(news("Beta guides lower", tickers=("BETA",)))
        assert [r["path"] for r in server.requests] == ["/push/b", "/push/b"]  # A is in digest mode
        assert [x["title"] for x in eng.desk.get(DEV)["digest_q"]] == [
            "Acme wins $2B Pentagon contract",
            "Beta guides lower",
        ]
        server.requests.clear()
        await eng.dispatcher.dispatch(
            news(
                "Market-wide halt", sev=__import__("news247.models", fromlist=["Severity"]).Severity.CRITICAL
            )
        )
        assert sorted(r["path"] for r in server.requests) == ["/push/a", "/push/b"]  # CRITICAL never waits
        server.requests.clear()
        assert await eng.send_digests() == 0  # not 15 minutes yet
        assert await eng.send_digests(time.time() + 15 * 60) == 1
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "📬 2 alerts (2 HIGH) · last 15 min"
        assert msg["body"].splitlines() == [
            "• Beta guides lower",
            "• Acme wins $2B Pentagon contract",
        ]  # newest first
        assert eng.desk.get(DEV)["digest_q"] == [] and eng.desk.view(DEV, lambda s: None)["held"] == 0
    finally:
        await eng.http.close()


def test_turning_digest_off_flushes_on_the_next_pass():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "prefs", {"digest": 60})
    d.get(DEV)["digest_at"] = time.time()
    a = news()
    assert d.hold(DEV, a) is True and d.hold(DEV, a) is True  # the same alert is held once
    assert len(d.get(DEV)["digest_q"]) == 1 and d.due_digests() == []
    d.apply(DEV, "prefs", {"digest": 0})
    assert d.hold(DEV, news("another")) is False
    assert [len(x) for _, x in d.due_digests()] == [1]


# --------------------------------------------------------------------------- moving a desk


def test_a_desk_moves_to_another_phone_with_a_code():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA"]})
    d.apply(DEV, "kw_add", {"keyword": "tariff"})
    d.apply(DEV, "note", {"symbol": "ACME", "text": "Wait for the pullback"})
    d.apply(DEV, "transfer_out", {})
    code = d.view(DEV, lambda s: None)["transfer"]["code"]
    assert len(code) == 8 and not set(code) & set("01IOL")
    with pytest.raises(ValueError, match="from this phone"):
        d.apply(DEV, "transfer_in", {"code": code})
    d.apply(DEV, "transfer_out", {})
    code = d.view(DEV, lambda s: None)["transfer"]["code"]
    d.apply(DEV2, "transfer_in", {"code": code.lower()[:4] + "-" + code.lower()[4:]})
    p2 = d.get(DEV2)
    assert (
        p2["watchlist"] == ["ACME", "BETA"]
        and p2["keywords"][0]["k"] == "tariff"
        and p2["notes"]["ACME"]["text"] == "Wait for the pullback"
    )
    p2["watchlist"].append("DLTA")
    assert d.get(DEV)["watchlist"] == ["ACME", "BETA"]  # a copy, not a shared object
    with pytest.raises(ValueError, match="wrong or expired"):
        d.apply(DEV2, "transfer_in", {"code": code})  # single use
    d.apply(DEV, "transfer_out", {})
    old = d.view(DEV, lambda s: None)["transfer"]["code"]
    d.transfers[old] = (DEV, time.time() - 1)
    with pytest.raises(ValueError, match="wrong or expired"):
        d.apply(DEV2, "transfer_in", {"code": old})


# --------------------------------------------------------------------------- notes, groups, votes, fades


def test_notes_groups_and_votes(tmp_path):
    st = Storage(tmp_path / "d.db")
    d = Desk(st)
    d.apply(DEV, "note", {"symbol": "acme", "text": "  Lockup ends Nov 3  "})
    assert d.get(DEV)["notes"]["ACME"]["text"] == "Lockup ends Nov 3"
    d.apply(DEV, "note", {"symbol": "ACME", "text": ""})
    assert "ACME" not in d.get(DEV)["notes"]
    with pytest.raises(ValueError, match="not on your watchlist"):
        d.apply(DEV, "watch_group", {"symbol": "ACME", "group": "Semis"})
    d.apply(DEV, "watch_add", {"symbol": "ACME"})
    d.apply(DEV, "watch_group", {"symbol": "ACME", "group": "  Earnings   plays "})
    assert d.view(DEV, lambda s: None)["groups"] == {"ACME": "Earnings plays"}
    a = news()
    st.add_alert(a)
    d.apply(DEV, "vote", {"id": a.id, "v": 1})
    d.apply(DEV2, "vote", {"id": a.id, "v": 1})
    assert st.get_alert(a.id)["edge"]["feedback"] == {"up": 2, "down": 0}
    d.apply(DEV2, "vote", {"id": a.id, "v": -1})  # changed their mind
    assert st.get_alert(a.id)["edge"]["feedback"] == {"up": 1, "down": 1}
    d.apply(DEV, "vote", {"id": a.id, "v": 0})
    assert st.get_alert(a.id)["edge"]["feedback"] == {"up": 0, "down": 1} and a.id not in d.get(DEV)["votes"]
    with pytest.raises(ValueError):
        d.apply(DEV, "vote", {"id": a.id, "v": 5})


def test_groups_that_fade_are_flagged():
    rates = {
        "options": {
            "h1": {"n": 25, "hit": 0.36, "median": -0.8},
            "close": {"n": 25, "hit": 0.44, "median": -0.2},
        }
    }
    b = base_rate(
        rates,
        SimpleNamespace(
            kind="options", severity=SimpleNamespace(name="HIGH"), tickers=["ACME"], edge={}, item=None
        ),
    )
    assert b["fades"] is True and base_line(b).endswith("· historically fades")
    rates["options"]["h1"]["n"] = 12  # too few to call it
    rates["options"]["close"]["n"] = 12
    assert (
        base_rate(
            rates,
            SimpleNamespace(
                kind="options", severity=SimpleNamespace(name="HIGH"), tickers=[], edge={}, item=None
            ),
        )["fades"]
        is False
    )


# --------------------------------------------------------------------------- earnings reminders and the open plan


async def test_earnings_reminder_before_the_report(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()

    async def down(url, **kw):
        raise aiohttp.ClientError("blocked")

    try:
        a, _ = phones(eng, server)
        eng.http.get = down
        eng.trends = SimpleNamespace(
            earnings=SimpleNamespace(
                lookup=lambda s, t=None: (
                    {"date": "2026-10-14", "when": "after the close", "in_days": 0} if s == "ACME" else None
                )
            )
        )
        eng.desk.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA"]})
        assert await eng.send_earnings_reminders(at(9, 0)) == 0  # an after-the-close report: not yet
        assert await eng.send_earnings_reminders(at(15, 40)) == 1
        assert await eng.send_earnings_reminders(at(15, 45)) == 0  # once
        msg = a.read(server.requests[0]["body"])
        assert (
            msg["title"] == "📅 ACME reports after the close"
            and msg["body"] == "On your watchlist"
            and msg["open"] == "/app/#t=ACME"
        )
        eng.desk.apply(DEV2, "prefs", {"earn_remind": False})
        eng.desk.apply(DEV2, "watch_add", {"symbol": "ACME"})
        assert await eng.send_earnings_reminders(at(15, 50)) == 0
    finally:
        await eng.http.close()


async def test_the_open_plan(tmp_path, server):
    from news247.models import Analysis, Severity

    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        now = at(9, 22)
        quotes(eng, now, ACME=4.0, BETA=0.4, GAMA=-7.0)
        news_item = item("Gamma Health trial misses primary endpoint", tickers=("GAMA",))
        news_item.detected = now - 3600
        eng._remember(news_item, Analysis(score=70, severity=Severity.HIGH, tickers=["GAMA"]))
        eng.desk.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA"]})
        assert await eng.send_open_plans(at(9, 10)) == 0
        assert await eng.send_open_plans(now) == 1
        assert await eng.send_open_plans(now + 60) == 0
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "🔔 The open in 10 minutes"
        assert msg["body"].splitlines()[:2] == [
            "★ ACME +4.0%",
            "GAMA -7.0%: Gamma Health trial misses primary endpoint",
        ]
    finally:
        await eng.http.close()
