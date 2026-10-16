"""Your desk (per phone: watchlist, push preferences, price alerts, tracked calls) and the views
built for the app: the ticker dossier with its evidence tally, the track record, smart money, the
market map and search."""

from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import aiohttp
import pytest
from aiohttp.test_utils import TestServer

from news247.desk import Desk, alert_category, clean_symbol, in_quiet
from news247.dossier import record_outcomes, search, sectors, smart_money, tally, ticker_dossier, track_record
from news247.market.radar import MoversRadar
from news247.market.universe import Universe
from news247.models import Alert, Analysis, NewsItem, Severity, SourceTier
from news247.storage import Storage
from news247.web.server import WebServer

from .test_webpush import Phone, push_engine

ET = ZoneInfo("America/New_York")
DEV = "dev_abcdef1234"
DEV2 = "dev_zyxwvu9876"
UNI = {
    "ACME": {"name": "Acme Corp Common Stock", "mcap": 5e9, "price": 100.0, "sector": "Technology"},
    "BETA": {
        "name": "Beta Industries Inc. Common Stock",
        "mcap": 20e9,
        "price": 50.0,
        "sector": "Technology",
    },
    "GAMA": {"name": "Gamma Health Inc. Common Stock", "mcap": 8e9, "price": 20.0, "sector": "Health Care"},
    "DLTA": {"name": "Delta Tech Corp Common Stock", "mcap": 1e9, "price": 10.0, "sector": "Technology"},
}


def news(
    title="Acme wins $2B Pentagon contract", tickers=("ACME",), sev=Severity.HIGH, direction="up", **extra
):
    item = NewsItem(source="globenewswire", title=title, url="https://x.test/1", tier=SourceTier.WIRE,
                    published=time.time() - 5, extra=dict(extra))  # fmt: skip
    an = Analysis(score=70, severity=sev, tickers=list(tickers), direction=direction)
    return Alert(
        kind="news",
        severity=sev,
        title=title,
        body="",
        url=item.url,
        tickers=list(tickers),
        item=item,
        analysis=an,
    )


def quote_book(prices: dict[str, tuple[float, float | None]]):
    return lambda sym: (time.time(), prices[sym][0], prices[sym][1]) if sym in prices else None


# --------------------------------------------------------------------------- the desk


def test_watchlist_prefs_alerts_and_tracked_calls(tmp_path):
    st = Storage(tmp_path / "d.db")
    d = Desk(st)
    book = quote_book({"ACME": (101.0, 1.0), "NVDA": (190.0, -2.5)})
    p = d.apply(DEV, "watch_add", {"symbols": ["acme", "$nvda", "bad symbol!!", "ACME"]})
    assert p["watchlist"] == ["ACME", "NVDA"]
    d.apply(DEV, "watch_remove", {"symbol": "nvda"})
    assert d.get(DEV)["watchlist"] == ["ACME"]
    d.apply(
        DEV,
        "prefs",
        {
            "cats": {"options": False, "bogus": False},
            "quiet": {"start": "22:00", "end": "07:00"},
            "watch_push": False,
        },
    )
    prefs = d.get(DEV)["prefs"]
    assert (
        prefs["cats"]["options"] is False
        and "bogus" not in prefs["cats"]
        and prefs["quiet"]["start"] == "22:00"
    )
    assert prefs["watch_push"] is False
    d.apply(DEV, "prefs", {"quiet": None})
    assert d.get(DEV)["prefs"]["quiet"] is None
    d.apply(DEV, "alert_add", {"symbol": "acme", "kind": "above", "value": 105})
    d.apply(DEV, "alert_add", {"symbol": "NVDA", "kind": "move", "value": 2})
    with pytest.raises(ValueError):
        d.apply(DEV, "alert_add", {"symbol": "ACME", "kind": "sideways", "value": 1})
    with pytest.raises(ValueError):
        d.apply(DEV, "alert_add", {"symbol": "ACME", "kind": "above", "value": "lots"})
    d.apply(DEV, "track_add", {"symbol": "ACME", "direction": "long"}, price_of=book)
    d.apply(DEV, "track_add", {"symbol": "NVDA", "direction": "short", "entry": 200}, price_of=book)
    with pytest.raises(ValueError, match="no live price"):
        d.apply(DEV, "track_add", {"symbol": "ZZZZ"}, price_of=book)
    v = d.view(DEV, book)
    assert [w["symbol"] for w in v["watchlist"]] == ["ACME"] and v["watchlist"][0]["chg_day"] == 1.0
    longs = {t["symbol"]: t for t in v["tracked"]}
    assert longs["ACME"]["entry"] == 101.0 and longs["ACME"]["pnl_pct"] == 0.0
    assert longs["NVDA"]["pnl_pct"] == 5.0  # short from $200, now $190
    tid = longs["NVDA"]["id"]
    d.apply(DEV, "track_close", {"id": tid}, price_of=book)
    v = d.view(DEV, book)
    assert (
        len(v["tracked"]) == 1
        and v["closed"][0]["pnl_pct"] == 5.0
        and v["record"]
        == {
            "closed": 1,
            "wins": 1,
            "avg_pnl_pct": 5.0,
            "by_tag": [{"tag": "untagged", "n": 1, "wins": 1, "avg": 5.0}],
        }
    )
    with pytest.raises(ValueError, match="unknown action"):
        d.apply(DEV, "launch_rockets", {})
    # saved: a new Desk on the same database has it all
    again = Desk(st)
    assert again.get(DEV)["watchlist"] == ["ACME"] and len(again.get(DEV)["price_alerts"]) == 2


def test_price_alerts_fire_once():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "alert_add", {"symbol": "ACME", "kind": "above", "value": 105})
    d.apply(DEV, "alert_add", {"symbol": "ACME", "kind": "below", "value": 90})
    d.apply(DEV2, "alert_add", {"symbol": "ACME", "kind": "move", "value": 5})
    assert d.due_price_alerts(quote_book({"ACME": (101.0, 1.0)})) == []
    fired = d.due_price_alerts(quote_book({"ACME": (106.0, 6.0)}))
    assert sorted((dev, a["kind"]) for dev, a, _ in fired) == [(DEV, "above"), (DEV2, "move")]
    assert d.due_price_alerts(quote_book({"ACME": (107.0, 7.0)})) == []  # once
    assert [a["kind"] for a in d.get(DEV)["price_alerts"] if not a["fired"]] == ["below"]


def test_categories_quiet_hours_and_who_watches():
    d = Desk(Storage(":memory:"))
    d.apply(DEV, "watch_add", {"symbol": "ACME"})
    d.apply(DEV, "prefs", {"cats": {"insider": False}})
    assert d.watchers(["acme", "ZZZ"]) == [DEV]
    assert d.allows(DEV, news()) is True
    ins = news("CEO buys $2M", insider_buy={"value": 2e6})
    assert alert_category(ins) == "insider" and d.allows(DEV, ins) is False
    assert d.allows("unknown_device1", ins) is True  # no profile: everything, as before
    macro = news("Fed cuts rates by 50bp", tickers=("SPY", "QQQ"), sev=Severity.CRITICAL)
    assert alert_category(macro) == "macro"
    assert alert_category(Alert(kind="options", severity=Severity.HIGH, title="t", body="")) == "options"
    radar = Alert(kind="price", severity=Severity.HIGH, title="t", body="", tickers=["ACME"])
    radar.edge["ignition"] = {"direction": "up"}
    assert alert_category(radar) == "radar" and alert_category(radar.to_dict()) == "radar"
    night = datetime(2026, 10, 14, 23, 30, tzinfo=ET).timestamp()
    noon = datetime(2026, 10, 14, 12, 0, tzinfo=ET).timestamp()
    q = {"start": "22:00", "end": "07:00"}
    assert in_quiet(q, night) and not in_quiet(q, noon) and not in_quiet(None, night)
    d.apply(DEV, "prefs", {"quiet": q})
    assert (
        d.allows(DEV, news(), night) is False and d.allows(DEV, macro, night) is True
    )  # CRITICAL gets through


def test_symbols_are_cleaned():
    assert clean_symbol(" $brk.b ") == "BRK.B" and clean_symbol("nvda") == "NVDA"
    assert (
        clean_symbol("1ABC") is None and clean_symbol("") is None and clean_symbol("TOOLONGSYMBOLX") is None
    )


# --------------------------------------------------------------------------- push respects the desk


async def test_each_phone_gets_only_what_its_desk_allows(tmp_path, server):
    server.on("/push/a", (201, ""))
    server.on("/push/b", (201, ""))
    eng = push_engine(tmp_path)
    await eng.http.start()
    try:
        a, b = Phone(server.url("/push/a")), Phone(server.url("/push/b"))
        sa, sb = a.subscription(), b.subscription()
        sa.device, sb.device = DEV, DEV2
        eng.webpush.subs = {sa.endpoint: sa, sb.endpoint: sb}  # type: ignore[union-attr]
        eng.desk.apply(DEV2, "prefs", {"cats": {"insider": False}})
        await eng.dispatcher.dispatch(news("CEO buys $2M of Acme", insider_buy={"value": 2e6}))
        paths = [r["path"] for r in server.requests]
        assert paths == ["/push/a"]  # B turned insider buys off
        # a MEDIUM alert on a watched stock goes to its watcher only (the phones' floor is HIGH)
        eng.desk.apply(DEV2, "watch_add", {"symbol": "ACME"})
        server.requests.clear()
        await eng._push_watchers(news("Acme gets FDA nod", sev=Severity.MEDIUM))
        assert [r["path"] for r in server.requests] == ["/push/b"]
        assert b.read(server.requests[-1]["body"])["title"].startswith("★ ")
        server.requests.clear()
        await eng._push_watchers(news("Beta news", tickers=("BETA",), sev=Severity.MEDIUM))
        assert server.requests == []
        # a price alert goes to its own phone
        eng.live_quotes.update("ACME", 106.0, 100.0, time.time(), "test")
        eng.desk.apply(DEV, "alert_add", {"symbol": "ACME", "kind": "above", "value": 105})
        server.requests.clear()
        fired = await eng.check_price_alerts()
        assert [d for d, _ in fired] == [DEV] and [r["path"] for r in server.requests] == ["/push/a"]
        shown = a.read(server.requests[-1]["body"])
        assert shown["title"] == "🔔 ACME is above $105.00" and shown["open"] == "/app/#t=ACME"
    finally:
        await eng.http.close()


# --------------------------------------------------------------------------- the views


def engine_with(tmp_path):
    eng = push_engine(tmp_path)
    uni = Universe.stub(UNI)
    eng.universe = uni
    eng.scorer.attach_universe(uni, **eng.cfg.smallcap.filters())
    if eng.radar is None:
        eng.radar = MoversRadar(eng.cfg.smallcap, uni)
    return eng


def test_price_of_prefers_the_freshest_source(tmp_path):
    eng = engine_with(tmp_path)
    now = time.time()
    eng.live_quotes.update("ACME", 101.0, 100.0, now - 30, "tape")
    assert eng.price_of("acme")[1] == 101.0 and eng.price_of("acme")[2] == pytest.approx(1.0)
    eng.detector.prev_close["ACME"] = 100.0
    eng.detector.last_price["ACME"] = (now, 102.0)
    assert eng.price_of("ACME")[1] == 102.0
    assert eng.price_of("NOPE") is None


def test_ticker_dossier_and_tally(tmp_path):
    eng = engine_with(tmp_path)
    now = time.time()
    a = news()
    a.edge["refs"] = {"ACME": 100.0}
    a.edge["play"] = {"direction": "up", "direct": ["ACME"]}
    eng.storage.add_alert(a)
    b = news("Acme downgraded to Sell at Big Bank", sev=Severity.MEDIUM, direction="down")
    eng.storage.add_alert(b)
    eng.options_seen["ACME"] = (now - 600, "up", "options: calls flow, $2.0M premium")
    item = NewsItem(source="sec-insider-buys", title="CEO Jane Doe buys $2.5M of Acme Corp (ACME) stock at $99 (Form 4, open market, opportunistic)",
                    url="https://sec.gov/x", tier=SourceTier.PRIMARY, tickers=["ACME"])  # fmt: skip
    eng.storage.add_item(item, Analysis(score=60, severity=Severity.HIGH, tickers=["ACME"], direction="up"))
    eng.live_quotes.update("ACME", 104.0, 100.0, now, "tape")
    eng.calendar.add({"id": "ACME:x", "date": datetime.fromtimestamp(now + 5 * 86400, ET).date().isoformat(),
                      "title": "ACME · Phase 3 readout", "kind": "binary", "symbol": "ACME"})  # fmt: skip
    eng.desk.apply(DEV, "watch_add", {"symbol": "ACME"})
    d = ticker_dossier(eng, "$acme", now)
    assert d["symbol"] == "ACME" and d["name"] == "Acme Corp Common Stock" and d["sector"] == "Technology"
    assert d["quote"]["price"] == 104.0 and d["quote"]["chg_day"] == pytest.approx(4.0)
    rows = {r["title"]: r for r in d["alerts"]}
    assert rows["Acme wins $2B Pentagon contract"]["since"] == pytest.approx(4.0)
    assert rows["Acme wins $2B Pentagon contract"]["direction"] == "up"
    t = d["tally"]
    kinds = sorted(s["kind"] for s in t["signals"])
    assert kinds == ["insider", "news", "news", "options"]
    assert t["bull"] == pytest.approx(2 + 3 + 2) and t["bear"] == pytest.approx(1) and t["lean"] == "bullish"
    assert d["insider"] and d["calendar"][0]["title"] == "ACME · Phase 3 readout" and d["watchers"] == 1
    assert ticker_dossier(eng, "not a symbol!") is None


def test_tally_leans():
    s = lambda side, w: {"side": side, "weight": w, "ts": 0, "kind": "news", "label": ""}  # noqa: E731
    assert tally([])["lean"] == "none"
    assert tally([s("up", 3), s("down", 1)])["lean"] == "bullish"
    assert tally([s("up", 2), s("down", 2)])["lean"] == "mixed"
    assert tally([s("down", 4)])["lean"] == "bearish"


def test_outcomes_are_stamped_and_aggregated(tmp_path):
    eng = engine_with(tmp_path)
    created = datetime(2026, 10, 14, 10, 0, tzinfo=ET).timestamp()
    up = news()
    up.created = created
    up.edge["refs"] = {"ACME": 100.0}
    up.edge["play"] = {"direction": "up", "direct": ["ACME"]}
    eng.storage.add_alert(up)
    dn = news("Beta guides down", tickers=("BETA",), direction="down")
    dn.created = created
    dn.edge["refs"] = {"BETA": 50.0}
    dn.edge["play"] = {"direction": "down", "direct": ["BETA"]}
    eng.storage.add_alert(dn)
    h1 = created + 3700
    eng.live_quotes.update("ACME", 102.0, 100.0, h1, "tape")
    eng.live_quotes.update("BETA", 51.0, 50.0, h1, "tape")
    assert record_outcomes(eng, h1) == 2
    close = datetime(2026, 10, 14, 16, 0, 30, tzinfo=ET).timestamp()
    eng.live_quotes.update("ACME", 103.0, 100.0, close, "tape")
    eng.live_quotes.update("BETA", 49.0, 50.0, close, "tape")
    assert record_outcomes(eng, close + 60) == 2
    assert record_outcomes(eng, close + 120) == 0  # stamped once
    o = eng.storage.get_alert(up.id)["edge"]["outcome"]["ACME"]
    assert o == {"h1": 2.0, "close": 3.0, "sign": 1}
    rec = track_record(eng, 30, now=close + 120)
    row = next(r for r in rec["rows"] if r["kind"] == "news:HIGH")
    assert row["h1"] == {"n": 2, "hit": 0.5, "median": 0.0}  # ACME +2 its way, BETA +2 against
    assert row["close"] == {"n": 2, "hit": 1.0, "median": 2.5}  # ACME +3, BETA -2% (its way: +2)


def test_smart_money_sectors_and_search(tmp_path):
    eng = engine_with(tmp_path)
    now = time.time()
    for sym, chg in (("ACME", 3.0), ("BETA", 1.0), ("DLTA", -2.0), ("GAMA", 0.5)):
        eng.live_quotes.update(sym, 100 * (1 + chg / 100), 100.0, now, "tape")
    m = sectors(eng, now)
    tech = next(r for r in m["rows"] if r["sector"] == "Technology")
    assert (
        tech["n"] == 3 and tech["leaders"][0]["symbol"] == "ACME" and tech["laggards"][0]["symbol"] == "DLTA"
    )
    assert tech["chg"] == pytest.approx((3 * 5 + 1 * 20 - 2 * 1) / 26, abs=0.01)
    assert all(r["sector"] != "Health Care" for r in m["rows"])  # one stock is not a sector
    item = NewsItem(
        source="sec-insider-buys",
        title="Director buys $1M of Beta (BETA) stock",
        url="u",
        tier=SourceTier.PRIMARY,
        tickers=["BETA"],
    )
    eng.storage.add_item(item, Analysis(score=40, severity=Severity.MEDIUM, tickers=["BETA"]))
    eng.radar.board["ACME"] = {
        "symbol": "ACME",
        "ignition": {
            "kind": "ignition",
            "direction": "up",
            "move_pct": 2.1,
            "burst": 6.0,
            "detected": now - 60,
            "primed": [],
        },
    }
    sm = smart_money(eng, now)
    assert sorted(r["kind"] for r in sm["rows"]) == ["ignition", "insider"]
    assert [r["ts"] for r in sm["rows"]] == sorted(
        (r["ts"] for r in sm["rows"]), reverse=True
    )  # newest first
    eng.storage.add_alert(news())
    found = search(eng, "pentagon", now)
    assert found["alerts"][0]["title"] == "Acme wins $2B Pentagon contract"
    assert search(eng, "acme", now)["companies"][0]["symbol"] == "ACME"
    assert search(eng, "beta ind", now)["companies"][0]["symbol"] == "BETA"
    assert search(eng, "a", now)["alerts"] == []


# --------------------------------------------------------------------------- the API


@pytest.fixture
async def api(tmp_path):
    eng = engine_with(tmp_path)
    await eng.http.start()
    srv = TestServer(WebServer(eng, eng.cfg.web).app)
    await srv.start_server()
    try:
        async with aiohttp.ClientSession() as s:
            yield eng, srv, s
    finally:
        await srv.close()
        await eng.http.close()


async def test_desk_and_views_over_the_api(api):
    eng, srv, s = api
    h = {"Authorization": "Bearer tok", "X-Device": DEV}
    async with s.get(srv.make_url("/api/desk"), headers={"Authorization": "Bearer tok"}) as r:
        assert r.status == 400  # no device id
    async with s.post(
        srv.make_url("/api/desk"), json={"action": "watch_add", "symbol": "ACME"}, headers=h
    ) as r:
        assert r.status == 200 and (await r.json())["watchlist"][0]["symbol"] == "ACME"
    async with s.post(
        srv.make_url("/api/desk"),
        json={"action": "alert_add", "symbol": "ACME", "kind": "nope", "value": 1},
        headers=h,
    ) as r:
        assert r.status == 400
    async with s.get(srv.make_url("/api/desk"), headers=h) as r:
        assert (await r.json())["prefs"]["cats"]["news"] is True
    async with s.get(srv.make_url("/api/ticker/acme"), headers=h) as r:
        d = await r.json()
        assert d["symbol"] == "ACME" and d["watching"] is True and d["tally"]["lean"] == "none"
    async with s.get(srv.make_url("/api/ticker/%21%21"), headers=h) as r:
        assert r.status == 400
    for path in ("/api/search?q=acme", "/api/smart", "/api/sectors", "/api/record?days=7"):
        async with s.get(srv.make_url(path), headers=h) as r:
            assert r.status == 200, path
    async with s.get(srv.make_url("/api/smart")) as r:
        assert r.status == 401
    # a subscription carries the phone's device id
    phone = Phone("https://web.push.apple.com/QGx")
    async with s.post(
        srv.make_url("/api/push/subscribe"),
        json={"subscription": phone.browser_json(), "label": "iPhone"},
        headers=h,
    ) as r:
        assert r.status == 200
    assert next(iter(eng.webpush.subs.values())).device == DEV  # type: ignore[union-attr]
