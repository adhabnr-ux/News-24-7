"""Run 11 of the desk: never-push words and source mutes, portfolio import and risk alerts, news
per holding, the day in a paragraph, and search filters."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from news247.desk import Desk
from news247.dossier import portfolio, search
from news247.models import Alert, Analysis, Severity
from news247.personal import day_summary
from news247.storage import Storage

from .test_desk import DEV, DEV2, news
from .test_personal import quotes, semis_engine
from .test_run3 import item, phones

ET = ZoneInfo("America/New_York")


def at(h, m, d=14):
    return datetime(2026, 10, d, h, m, tzinfo=ET).timestamp()


def test_never_push_words_and_muted_sources():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "neg_add", {"keyword": "Crypto"})
    with pytest.raises(ValueError):
        d.apply(DEV, "neg_add", {"keyword": "x"})
    assert d.allows(DEV, news("Acme adds crypto to its treasury")) is False
    assert d.allows(DEV, news("Acme wins a contract")) is True
    assert (
        d.allows(DEV, news("Crypto exchange halts withdrawals", sev=Severity.CRITICAL)) is True
    )  # CRITICAL always comes
    d.apply(DEV, "prefs", {"mute_source": "globenewswire"})
    assert d.allows(DEV, news("Acme wins a contract")) is False  # news() items come from globenewswire
    d.apply(DEV, "prefs", {"mute_source": "globenewswire", "on": False})
    d.apply(DEV, "neg_remove", {"keyword": "crypto"})
    assert d.allows(DEV, news("Acme adds crypto to its treasury")) is True
    assert d.view(DEV, lambda s: None)["kw_neg"] == []


async def test_keyword_pushes_respect_never_push_words(tmp_path):
    eng = semis_engine(tmp_path)
    eng.desk.apply(DEV, "kw_add", {"keyword": "tariff"})
    eng.desk.apply(DEV, "neg_add", {"keyword": "rumor"})
    pushed = []

    async def to_device(device, payload, urgency="high"):
        pushed.append(payload["title"])
        return True

    eng._to_device = to_device
    await eng._keyword_push(
        item("Tariff rumor swirls"), Analysis(score=30, severity=Severity.MEDIUM), story_id=1
    )
    await eng._keyword_push(
        item("New tariff on chips"), Analysis(score=30, severity=Severity.MEDIUM), story_id=2
    )
    assert pushed == ["🔑 tariff: New tariff on chips"]


def test_portfolio_import_from_pasted_lines():
    d = Desk(Storage(":memory:"))
    d.apply(
        DEV,
        "hold_import",
        {"text": "Symbol,Shares,Cost\nAAPL, 10, $150\nmsft\t5\nNVDA 2.5 120.5\nbad line here\nTSLA -3 100\n"},
    )
    h = d.get(DEV)["holdings"]
    assert h == {
        "AAPL": {"shares": 10.0, "cost": 150.0},
        "NVDA": {"shares": 2.5, "cost": 120.5},
        "MSFT": {"shares": 5.0, "cost": None},
    }
    assert (
        d.view(DEV, lambda s: None)["imported"] == 3 and d.view(DEV, lambda s: None)["imported"] is None
    )  # said once
    with pytest.raises(ValueError, match="no lines"):
        d.apply(DEV, "hold_import", {"text": "hello"})


async def test_portfolio_risk_alerts(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        now = at(11, 0)
        quotes(eng, now, ACME=-3.0, GAMA=-1.0)
        eng.desk.apply(DEV, "hold_set", {"symbol": "ACME", "shares": 10})
        eng.desk.apply(DEV, "hold_set", {"symbol": "GAMA", "shares": 10})
        with pytest.raises(ValueError):
            eng.desk.apply(DEV, "prefs", {"port_alert": 4})
        eng.desk.apply(DEV, "prefs", {"port_alert": 1})
        assert await eng.check_portfolio_risk(now) == 1  # -2.6%: past 2x the line at once
        msg = a.read(server.requests[0]["body"])
        assert (
            msg["title"].startswith("📉 Your portfolio -")
            and "ACME −$30" in msg["body"]
            and "±2%" in msg["body"]
        )
        assert await eng.check_portfolio_risk(now + 60) == 0  # both lines spent for today
        assert await eng.check_portfolio_risk(at(23, 0)) == 0  # market closed
    finally:
        await eng.http.close()


def test_holdings_carry_their_news_and_the_day_reads_as_a_paragraph(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    quotes(eng, now, ACME=6.0, BETA=-2.0, SPY=0.5)
    it = item("Acme wins $2B Pentagon contract")
    it.detected = now - 600
    eng._remember(it, Analysis(score=70, severity=Severity.HIGH, tickers=["ACME"]))
    eng.desk.apply(DEV, "hold_set", {"symbol": "ACME", "shares": 10})
    eng.desk.apply(DEV, "watch_add", {"symbols": ["ACME", "BETA"]})
    row = portfolio(eng, DEV, now)["rows"][0]
    assert row["news"]["title"] == "Acme wins $2B Pentagon contract"
    text = day_summary(eng, DEV, now)
    assert text.startswith(
        "1 of your 2 watched stocks rose; ACME led (+6.0%) on “Acme wins $2B Pentagon contract”, BETA lagged (-2.0%)."
    )
    assert "Your portfolio is +6.0% today (+$60)." in text and "The market: SPY +0.5%" in text
    assert day_summary(eng, DEV2, now).startswith("The market:")


def test_search_filters(tmp_path):
    eng = semis_engine(tmp_path)
    now = time.time()
    for sev, title in ((Severity.HIGH, "Acme high"), (Severity.MEDIUM, "Acme medium")):
        a = news(title, sev=sev)
        a.created = now - 3600
        eng.storage.add_alert(a)
    opt = Alert(
        kind="options",
        severity=Severity.HIGH,
        title="Acme calls bought",
        body="",
        tickers=["ACME"],
        created=now - 3600,
    )
    eng.storage.add_alert(opt)
    old = news("Acme old")
    old.created = now - 10 * 86400
    eng.storage.add_alert(old)
    titles = lambda r: sorted(a["title"] for a in r["alerts"])  # noqa: E731
    assert titles(search(eng, "acme", now)) == ["Acme calls bought", "Acme high", "Acme medium", "Acme old"]
    assert titles(search(eng, "acme", now, min_sev="HIGH")) == ["Acme calls bought", "Acme high", "Acme old"]
    assert titles(search(eng, "acme", now, kind="options")) == ["Acme calls bought"]
    assert titles(search(eng, "acme", now, days=1)) == ["Acme calls bought", "Acme high", "Acme medium"]
