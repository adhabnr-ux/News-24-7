"""The live options tape (market/live.py + the live parts of market/options.py).

The case it answers: Oct 12 2026, BIIB gapped up at the open and its $247.5 calls went from
$0.06 to $0.80; the alert came after the run, with Friday's stock price ($225.71, +3.33%) on it.
Two causes, both tested here: option trades on the free feed print ~15 min late (the live bid
does not), and the chain's stock price came from a universe that still showed Friday.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

import msgpack
import pytest

from news247.http import HttpClient
from news247.market.live import (
    LiveQuotes,
    OptionStream,
    Watch,
    parse_stock_snapshots,
    pick_watch,
)
from news247.market.options import Chain, Contract, OptionsFeed, OptionsRadar
from news247.market.universe import Listing, Universe

from .conftest import Recorder
from .test_options import ET, alpaca_cfg, engine, li, ocfg, uni


@pytest.fixture(autouse=True)
def _no_alpaca_env(monkeypatch):
    for name in ("ALPACA_KEY", "ALPACA_SECRET", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)


def utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")


# --------------------------------------------------------------------------- live stock prices


def test_live_quotes_newest_wins_and_movers_need_fresh_prices():
    lq = LiveQuotes()
    now = time.time()
    lq.update("biib", 233.98, 225.71, now - 10, "alpaca")
    lq.update("BIIB", 230.0, 225.71, now - 60, "yahoo sweep")  # older: ignored
    q = lq.get("BIIB", 180, now)
    assert q is not None and q.price == 233.98 and q.source == "alpaca"
    assert q.change_pct == pytest.approx(3.663, abs=0.001)
    lq.update("FLAT", 100.0, 99.9, now, "yahoo sweep")
    lq.update("OLD", 50.0, 40.0, now - 600, "yahoo sweep")
    assert lq.movers(3.0, 180, now) == ["BIIB"]  # OLD moved 25% but its quote is stale
    assert lq.get("OLD", 180, now) is None and lq.get("NOPE", 180, now) is None
    lq.update("X", 0, 1, now, "s")  # a zero price is not a quote
    assert lq.get("X", 180, now) is None
    assert lq.status(now)["fresh_3m"] == 2


def test_alpaca_stock_snapshots_in_session_and_before_the_first_trade():
    today = date(2026, 10, 12)
    day0 = datetime(2026, 10, 12, tzinfo=ET)
    doc = {
        "BIIB": {  # in session: today's bar exists, the previous close is Friday's
            "latestTrade": {"t": utc(day0.replace(hour=9, minute=31)), "p": 233.98},
            "dailyBar": {"t": utc(day0), "c": 233.98},
            "prevDailyBar": {"t": utc(day0 - timedelta(days=3)), "c": 225.71},
        },
        "QUIET": {  # before its first trade today: the daily bar is still Friday's
            "latestTrade": {"t": utc(day0 - timedelta(days=3, hours=-15)), "p": 50.0},
            "dailyBar": {"t": utc(day0 - timedelta(days=3)), "c": 50.0},
            "prevDailyBar": {"t": utc(day0 - timedelta(days=4)), "c": 48.0},
        },
        "BAD": {"latestTrade": {}},
    }
    got = parse_stock_snapshots(doc, today)
    assert got["BIIB"][:2] == (233.98, 225.71) and got["QUIET"][:2] == (50.0, 50.0)
    assert "BAD" not in got and parse_stock_snapshots(None, today) == {}
    assert parse_stock_snapshots({"snapshots": doc}, today)["BIIB"][0] == 233.98  # both shapes


def biib_chain(stock_source: str = "universe") -> Chain:
    """Alpaca chains carry the universe's stock price: on Oct 12 that was Friday's."""
    return Chain(
        symbol="BIIB",
        price=225.71,
        change_pct=3.33,
        iv30=0.3,
        source="Alpaca test",
        stock_source=stock_source,
    )


def test_live_price_replaces_the_universe_and_a_stale_universe_says_nothing():
    u = Universe([li("BIIB", 33e9, 3.33)], loaded_at=time.time() - 3600, source="test")
    f = OptionsFeed(ocfg(), None, u, OptionsRadar(ocfg()))  # type: ignore[arg-type]
    now = time.time()
    ch = biib_chain()
    f.apply_live(ch, now)  # no live quote: the universe's figure is Friday's close, not today's price
    assert ch.price is None and ch.change_pct is None
    assert ch.prev_close == 225.71 and ch.stock_prev == 225.71  # ... so it is yesterday's price
    assert ch.stock_source.startswith("no live price")
    f.live.update("BIIB", 233.98, 225.71, now - 5, "alpaca")
    ch = biib_chain()
    f.apply_live(ch, now)
    assert (ch.price, ch.prev_close) == (233.98, 225.71) and ch.change_pct == pytest.approx(3.66, abs=0.01)
    assert ch.stock_source == "alpaca" and ch.stock_time is not None
    assert ch.stock_prev == 225.71  # the model base is Friday's close, not Thursday's


def test_hot_list_reads_live_movers_not_the_ten_minute_universe():
    u = uni(li("BIIB", 33e9, 0.0), li("BIG", 900e9, 0.1))  # the universe hasn't seen the gap
    f = OptionsFeed(ocfg(), None, u, OptionsRadar(ocfg()))  # type: ignore[arg-type]
    now = time.time()
    assert f.hot(now) == []
    f.live.update("BIIB", 233.98, 225.71, now, "yahoo sweep")
    assert f.hot(now) == ["BIIB"]


# --------------------------------------------------------------------------- quote-led spikes


def contract(
    now: float, *, bid: float, ask: float, last: float, last_age_s: float, quote_age_s: float, **kw: Any
) -> Contract:
    t_now = datetime.fromtimestamp(now, ET)
    return Contract(
        symbol="BIIB261016C00247500",
        underlying="BIIB",
        expiry=kw.pop("expiry", date(2026, 10, 16)),
        cp="C",
        strike=247.5,
        bid=bid,
        ask=ask,
        last=last,
        prev_close=kw.pop("prev_close", 0.06),
        volume=kw.pop("volume", 3),
        open_interest=kw.pop("open_interest", 120),
        iv=0.45,
        last_trade=t_now - timedelta(seconds=last_age_s),
        quote_time=t_now - timedelta(seconds=quote_age_s) if quote_age_s is not None else None,
        **kw,
    )


SOON = date.today() + timedelta(days=4)  # for the tests that run on the real clock: never already expired
NOW = datetime(2026, 10, 12, 9, 34, tzinfo=ET).timestamp()  # four minutes after the gap


def live_chain(c: Contract) -> Chain:
    ch = Chain(
        symbol="BIIB", price=233.98, prev_close=225.71, change_pct=3.66, iv30=0.3, source="Alpaca test"
    )
    ch.contracts.append(c)
    ch.quote_time = c.quote_time
    return ch


def test_the_live_bid_catches_the_spike_before_the_trades_print():
    radar = OptionsRadar(ocfg())
    # 09:34: the bid is already $0.70 (+1,067%), the feed's last trade is still the $0.08 print
    # from 15 minutes ago, and only 3 contracts have printed so far
    c = contract(NOW, bid=0.70, ask=0.95, last=0.08, last_age_s=900, quote_age_s=4)
    (hit,) = radar.scan(live_chain(c), Listing("BIIB", "Biogen", market_cap=33e9), NOW)
    top = hit.spikes[0]
    assert top.quote_led and top.real and hit.severity == "HIGH"
    assert top.move_pct == pytest.approx(1066.7, abs=0.1)


def test_no_live_bid_no_quote_led_spike():
    radar = OptionsRadar(ocfg())
    stale = contract(NOW, bid=0.70, ask=0.95, last=0.08, last_age_s=900, quote_age_s=600)
    assert radar.scan(live_chain(stale), None, NOW) == []  # a 10-minute-old quote is not live
    cboe = contract(NOW, bid=0.70, ask=0.95, last=0.08, last_age_s=900, quote_age_s=None)
    assert radar.scan(live_chain(cboe), None, NOW) == []  # Cboe gives no quote time: all delayed
    small = contract(NOW, bid=0.40, ask=0.95, last=0.08, last_age_s=900, quote_age_s=4)
    assert OptionsRadar(ocfg()).scan(live_chain(small), None, NOW) == []  # +567%: under 1,000%
    penny = contract(NOW, bid=0.05, ask=0.09, last=0.01, last_age_s=900, quote_age_s=4, prev_close=0.004)
    assert OptionsRadar(ocfg()).scan(live_chain(penny), None, NOW) == []  # under $0.10: noise


def test_printed_trades_still_lead_when_they_are_fresher_and_higher():
    radar = OptionsRadar(ocfg())
    c = contract(NOW, bid=0.70, ask=0.95, last=0.90, last_age_s=30, quote_age_s=4, volume=200)
    (hit,) = radar.scan(live_chain(c), None, NOW)
    assert not hit.spikes[0].quote_led and hit.spikes[0].move_pct == pytest.approx(1400.0)


# --------------------------------------------------------------------------- the tripwire


def stream(**kw: Any) -> tuple[OptionStream, list]:
    fired: list = []
    st = OptionStream("wss://x/v1beta1/indicative", "k", "s", lambda *a: fired.append(a), **kw)
    return st, fired


def q(occ: str, bid: float, t: Any = None) -> dict:
    return {"T": "q", "S": occ, "bp": bid, "ap": bid + 0.1, "t": t}


def test_stream_fires_each_rung_once_and_again_next_session():
    st, _ = stream()
    st.set_candidates("BIIB", [Watch("BIIB261016C00247500", "BIIB", 0.06, rank=5)])
    now = datetime(2026, 10, 12, 9, 31, tzinfo=ET).timestamp()
    assert st.handle([q("BIIB261016C00247500", 0.10)], now) == []  # +67%: not a rung
    (f1,) = st.handle([q("BIIB261016C00247500", 0.25)], now)  # +317%: the 4x rung
    assert f1[:2] == ("BIIB", "BIIB261016C00247500") and f1[3] == pytest.approx(316.7, abs=0.1)
    assert st.handle([q("BIIB261016C00247500", 0.26)], now) == []  # same rung: once
    (f2,) = st.handle([q("BIIB261016C00247500", 0.70)], now)  # +1,067%: the 11x rung
    assert f2[3] > 1000 and st.stats.triggers == 2
    assert st.handle([q("OTHER", 9.0)], now) == []  # not watched
    tomorrow = now + 86400
    assert len(st.handle([q("BIIB261016C00247500", 0.25)], tomorrow)) == 1  # a new session


def test_stream_quote_lag_and_timestamps():
    st, _ = stream()
    st.set_candidates("X", [Watch("X1", "X", 1.0)])
    now = time.time()
    ts = msgpack.Timestamp.from_unix(now - 0.4)
    st.handle([q("X1", 1.0, ts)], now)
    st.handle([q("X1", 1.0, datetime.fromtimestamp(now - 1.0, timezone.utc).isoformat())], now)
    assert st.stats.to_dict()["quote_lag_median_s"] == pytest.approx(1.0, abs=0.05)


def test_stream_errors_subscriptions_and_the_symbol_limit():
    st, _ = stream(max_quotes=40)
    st.set_candidates("A", [Watch(f"A{i}", "A", 1.0, rank=i) for i in range(30)])
    st.set_candidates("B", [Watch(f"B{i}", "B", 1.0, rank=100 + i) for i in range(30)])
    assert len(st.watch) == 40 and all(f"B{i}" in st.watch for i in range(30))  # best ranks kept
    st.handle([{"T": "subscription", "quotes": ["B1", "B2"]}])
    assert st.subscribed == {"B1", "B2"}
    with pytest.raises(ConnectionError, match="405"):
        st.handle([{"T": "error", "code": 405, "msg": "symbol limit exceeded"}])
    assert st.max_quotes == 30 and len(st.watch) == 30
    with pytest.raises(ConnectionError, match="402"):
        st.handle([{"T": "error", "code": 402, "msg": "auth failed"}])
    st.drop("B")
    assert not any(w.underlying == "B" for w in st.watch.values())


def test_watch_picks_cheap_near_dated_contracts_already_running_first():
    today = date(2026, 10, 12)
    mk = lambda occ, exp, k, prev, bid: Contract(occ, "BIIB", exp, "C", k, bid=bid, prev_close=prev)  # noqa: E731
    rows = [
        mk("RUN", date(2026, 10, 16), 247.5, 0.06, 0.30),  # already 5x
        mk("NEAR", date(2026, 10, 16), 235.0, 0.50, 0.55),
        mk("LATER", date(2026, 10, 30), 240.0, 0.80, 0.85),
        mk("FAR", date(2026, 12, 18), 240.0, 1.50, 1.55),  # 67 days: not explosive
        mk("DEAR", date(2026, 10, 16), 220.0, 9.0, 13.0),  # $9: not a lottery ticket
        mk("WAY", date(2026, 10, 16), 320.0, 0.05, 0.05),  # 37% out: never reached
        mk("PENNY", date(2026, 10, 16), 250.0, 0.01, 0.02),  # a penny base is noise
    ]
    got = pick_watch(rows, "BIIB", 233.98, today, per_symbol=3)
    assert [w.occ for w in got] == ["RUN", "NEAR", "LATER"]


class FakeWS:
    def __init__(self, frames: list[Any]) -> None:
        self.frames = [msgpack.packb(f, use_bin_type=True) for f in frames]
        self.sent: list[Any] = []

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(msgpack.unpackb(data, raw=False))

    async def receive(self, timeout: float | None = None):
        import aiohttp

        class M:
            pass

        m = M()
        if self.frames:
            m.type, m.data = aiohttp.WSMsgType.BINARY, self.frames.pop(0)
        else:
            await asyncio.sleep(0.01)
            m.type, m.data = aiohttp.WSMsgType.CLOSED, None
        return m

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, ws: FakeWS) -> None:
        self.ws = ws
        self.calls: list[tuple[str, dict]] = []

    def ws_connect(self, url: str, **kw: Any) -> FakeWS:
        self.calls.append((url, kw))
        return self.ws


async def test_stream_authenticates_subscribes_and_triggers_an_urgent_read():
    u = uni(li("BIIB", 33e9, 3.0))
    cfg = ocfg(alpaca_key="PK", alpaca_secret="SK", provider="alpaca", fallback=False)
    f = OptionsFeed(cfg, None, u, OptionsRadar(cfg))  # type: ignore[arg-type]
    assert f.stream is not None and f.stream.url == "wss://stream.data.alpaca.markets/v1beta1/indicative"
    occ = "BIIB261016C00247500"
    ws = FakeWS([
        [{"T": "success", "msg": "connected"}],
        [{"T": "success", "msg": "authenticated"}],
        [{"T": "subscription", "quotes": [occ]}],
        [q(occ, 0.70, msgpack.Timestamp.from_unix(time.time()))],
    ])  # fmt: skip
    session = FakeSession(ws)
    f.stream._session = lambda: session
    f.stream.set_candidates("BIIB", [Watch(occ, "BIIB", 0.06, rank=9)])
    with pytest.raises(ConnectionError):  # the fake socket closes after its frames
        await f.stream._session_run(asyncio.Event())
    assert session.calls[0][1]["headers"] == {"Content-Type": "application/msgpack"}
    assert ws.sent[0] == {"action": "auth", "key": "PK", "secret": "SK"}
    assert ws.sent[1] == {"action": "subscribe", "quotes": [occ]}
    assert "BIIB" in f._urgent and "$247.5 call (Oct 16) hit $0.70" in f._urgent["BIIB"]
    assert f.stream.stats.triggers == 1 and f.stream.stats.connects == 1


async def test_stream_waits_for_the_session_and_reports_a_missing_msgpack(monkeypatch):
    st, _ = stream(is_open=lambda: False)
    st.set_candidates("X", [Watch("X1", "X", 1.0)])
    stop = asyncio.Event()
    task = asyncio.create_task(st.run(stop))
    await asyncio.sleep(0.05)
    assert not st.stats.connected and st.stats.connects == 0  # closed market: no connection
    stop.set()
    await asyncio.wait_for(task, 2)
    import builtins

    real = builtins.__import__

    def no_msgpack(name, *a, **k):
        if name == "msgpack":
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_msgpack)
    st2, _ = stream()
    await st2.run(asyncio.Event())
    assert "msgpack" in st2.stats.last_error


# --------------------------------------------------------------------------- the urgent lane


class FakeSource:
    name = "fake"
    label = "Fake live feed"
    interval_s = 0.0

    def __init__(self, chains: dict[str, Chain]) -> None:
        self.chains = chains
        self.reads: list[str] = []

    async def fetch(self, symbol: str, listing: Any, pace: Any) -> Chain:
        self.reads.append(symbol)
        return self.chains[symbol]


async def test_urgent_names_are_read_at_once_with_the_reason_on_the_alert():
    now = time.time()
    c = contract(now, bid=0.70, ask=0.95, last=0.08, last_age_s=900, quote_age_s=2, expiry=SOON)
    ch = live_chain(c)
    ch.quote_time = c.quote_time
    src = FakeSource({"BIIB": ch})
    u = uni(li("BIIB", 33e9, 3.0), li("BIG", 900e9))
    f = OptionsFeed(
        ocfg(urgent_gap_s=20), None, u, OptionsRadar(ocfg()), sources=[src], market_phase=lambda t: "open"
    )
    got: list = []

    async def on_hits(hits):
        got.extend(hits)

    stop = asyncio.Event()
    f._urgent_wake = asyncio.Event()
    loop = asyncio.create_task(f._urgent_loop(on_hits, stop))
    f._on_trigger("BIIB", "BIIB261016C00247500", 0.70, 1066.7)
    for _ in range(100):
        if got:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(loop, 7)
    (hit,) = got
    assert src.reads == ["BIIB"] and f.urgent_reads == 1 and hit.spikes[0].quote_led
    assert any("read at once: the live bid on the $247.5 call" in r for r in hit.reasons)
    assert f.urgent(["BIIB"]) == 0  # read seconds ago: not again inside urgent_gap_s
    assert f.urgent(["TINY", ""]) == 0  # not an eligible company
    assert "BIIB" not in f.next_batch(time.time(), 5)  # counted as read: the rotation skips it


async def test_reading_a_hot_chain_puts_its_contracts_on_the_tripwire():
    now = time.time()
    c = contract(now, bid=0.10, ask=0.15, last=0.08, last_age_s=60, quote_age_s=2, expiry=SOON)
    ch = live_chain(c)
    u = uni(li("BIIB", 33e9, 3.0))
    cfg = ocfg(alpaca_key="PK", alpaca_secret="SK", provider="alpaca", fallback=False)
    f = OptionsFeed(cfg, None, u, OptionsRadar(cfg), sources=[FakeSource({"BIIB": ch})])
    f.stream = OptionStream("wss://x", "k", "s", f._on_trigger)
    f.live.update("BIIB", 233.98, 225.71, now, "alpaca")
    await f.read("BIIB", now)
    assert "BIIB261016C00247500" in f.stream.watch  # moving 3.7%: watched
    f.live.update("BIIB", 226.0, 225.71, now + 1, "alpaca")  # cooled to +0.1%
    await f.read("BIIB", now + 2)
    assert not f.stream.watch


async def test_alpaca_stock_snapshots_feed_the_live_store(server: Recorder, http: HttpClient):
    from aiohttp import web

    day0 = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
    seen: list[dict] = []

    def snaps(request):
        seen.append(dict(request.query))
        return web.json_response({
            "BIIB": {
                "latestTrade": {"t": utc(datetime.now(ET) - timedelta(seconds=3)), "p": 233.98},
                "dailyBar": {"t": utc(day0), "c": 233.98},
                "prevDailyBar": {"t": utc(day0 - timedelta(days=1)), "c": 225.71},
            }
        })  # fmt: skip

    server.on("/v2/stocks/snapshots", snaps)
    cfg = alpaca_cfg(server)
    f = OptionsFeed(cfg, http, uni(li("BIIB", 33e9)), OptionsRadar(cfg))
    assert await f.refresh_stocks(["BIIB", "BRK-B"]) == 1
    assert seen[0]["symbols"] == "BIIB,BRK.B" and seen[0]["feed"] == "iex"
    assert server.requests[-1]["headers"].get("APCA-API-KEY-ID") == "PKTEST"
    q1 = f.live.get("BIIB", 180)
    assert q1 is not None and q1.prev_close == 225.71 and q1.source == "alpaca"
    st = f.status()["live"]
    assert st["stock_snapshot_reads"] == 1 and st["stream"]["enabled"] is True


async def test_no_alpaca_keys_no_stream_and_no_snapshots(http: HttpClient):
    f = OptionsFeed(ocfg(), http, uni(li("BIIB", 33e9)), OptionsRadar(ocfg()))
    assert f.stream is None and await f.refresh_stocks(["BIIB"]) == 0
    assert f.status()["live"]["stream"] == {"enabled": False, "why": "needs Alpaca keys"}


# --------------------------------------------------------------------------- the engine


async def test_the_radar_sweep_shares_prices_and_news_reads_chains_at_once(tmp_path, monkeypatch):
    from news247.market.radar import Quote
    from news247.models import Alert, Severity

    eng, _ = engine(tmp_path)
    assert eng.options_feed.live is eng.live_quotes and eng.smallcap_feed.live is eng.live_quotes
    eng.smallcap_feed._share(
        [
            Quote("XYZ", price=52.0, prev_close=50.0, session="regular"),
            Quote("PRE", price=9.0, session="pre"),
        ],
        "yahoo sweep",
    )
    assert eng.live_quotes.get("XYZ", 60) is not None and eng.live_quotes.get("PRE", 60) is None
    monkeypatch.setattr(
        eng.calendar, "market_status", lambda now: {"open": True, "phase": "open", "until_s": 3600}
    )
    await eng.publish(
        Alert(kind="news", severity=Severity.LOW, title="Xylo wins FDA nod", body="", tickers=["XYZ"])
    )
    assert eng.options_feed._urgent.get("XYZ", "").startswith("in the news: Xylo wins FDA nod")


async def test_quote_led_alert_says_how_it_was_caught(tmp_path):
    eng, cap = engine(tmp_path)
    now = time.time()
    c = contract(now, bid=0.70, ask=0.95, last=0.08, last_age_s=900, quote_age_s=4, expiry=SOON)
    c.underlying = "XYZ"
    c.symbol = "XYZ261016C00247500"
    ch = live_chain(c)
    ch.symbol = "XYZ"
    ch.stock_source, ch.stock_time = "alpaca", datetime.fromtimestamp(now - 3, ET)
    hits = eng.options_radar.scan(ch, eng.universe.get("XYZ"), now)
    await eng.on_options(hits)
    await asyncio.sleep(0.05)
    a = cap.alerts[0]
    assert "$0.06 → bid $0.70 live (bid $0.70 / ask $0.95) · last print $0.08, 3 contracts so far" in a.body
    assert "Data: quote 4s old, last trade 15 min old" in a.body
    assert "trades print about 15 minutes late, the bid does not" in a.body
    assert "stock price: alpaca" in a.body


class FakeAlpaca(FakeSource):
    name = "alpaca"

    def __init__(self, prices: dict[str, tuple[float, float]]) -> None:
        super().__init__({})
        self.prices = prices
        self.asked: list[list[str]] = []

    async def stock_snapshots(self, symbols: list[str], pace: Any) -> dict:
        self.asked.append(list(symbols))
        now = time.time()
        return {s: (*self.prices[s], now) for s in symbols if s in self.prices}


async def test_the_stock_sweep_sees_a_gap_on_any_covered_name_and_reads_it_at_once():
    u = uni(li("BIIB", 33e9), li("MIDB", 1.4e9), li("FLAT", 5e9), li("SMALL", 400e6))
    src = FakeAlpaca({"BIIB": (233.98, 225.71), "MIDB": (11.0, 10.0), "FLAT": (50.1, 50.0)})
    f = OptionsFeed(ocfg(stock_sweep_s=120), None, u, OptionsRadar(ocfg()), sources=[src])
    now = time.time()
    new = await f.sweep_stocks(now)
    assert sorted(src.asked[0]) == ["BIIB", "FLAT", "MIDB"]  # every covered company, not the $400M one
    assert new == ["MIDB", "BIIB"]  # biggest move first
    assert f._urgent["MIDB"] == "moving +10.0% on a live price" and "BIIB" in f._urgent
    assert await f.sweep_stocks(now + 30) == []  # not due yet
    src.prices["FLAT"] = (53.0, 50.0)
    f._urgent.clear()
    assert await f.sweep_stocks(now + 121) == ["FLAT"]  # only the name that started moving
    assert f.status()["live"]["moving_now"] == 3
    assert await OptionsFeed(ocfg(), None, u, OptionsRadar(ocfg())).sweep_stocks(now) == []  # no Alpaca
