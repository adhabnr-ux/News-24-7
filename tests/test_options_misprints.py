"""Misprinted options (market/options.py OptionsRadar.misprint_rows and the stream path).

The case behind it (Oct 12 2026, OPRA bars via TradingView): BIIB's $252.5 call (Oct 16), with no
trade before today, showed $0.01 on a broker while it printed $0.65 at the open, one contract at
$0.20 at 10:45 ET, then 200 at $0.76-$0.85 at 11:05, on a $0.15 / $1.25 market. The chains below
are built from a smooth implied-vol curve (55%) so every misprint is known exactly.
"""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from news247.market.live import OptionStream, Watch
from news247.market.options import (
    Chain,
    Contract,
    OptionsFeed,
    OptionsRadar,
    bs_price,
    implied_vol,
    years_left,
)
from news247.market.universe import Listing

from .test_options import ET, engine, li, ocfg, uni

IV = 0.55
S = 234.47


def setup(now: float | None = None) -> tuple[float, date, date]:
    now = now or datetime.now(ET).replace(hour=11, minute=30, second=0, microsecond=0).timestamp()
    today = datetime.fromtimestamp(now, ET).date()
    return now, today, today + timedelta(days=4)


def k(
    now: float,
    exp: date,
    strike: float,
    cp: str = "C",
    *,
    bid: float | None = None,
    ask: float | None = None,
    spread: float = 0.10,
    quote_age: float | None = 2.0,
    last: float | None = None,
    last_at: datetime | None = None,
    sym: str = "BIIB",
    ask_size: int | None = 3,
) -> Contract:
    """A contract quoted around its fair value on the 55% curve unless bid/ask are given."""
    fair = bs_price(S, strike, years_left(exp, now), IV, cp)
    b = bid if bid is not None else max(round(fair - spread / 2, 2), 0.01)
    a = ask if ask is not None else round(fair + spread / 2, 2)
    occ = f"{sym}{exp:%y%m%d}{cp}{int(strike * 1000):08d}"
    return Contract(
        occ,
        sym,
        exp,
        cp,
        strike,
        bid=b,
        ask=a,
        last=last,
        prev_close=max(round(fair, 2), 0.01),  # yesterday near today's value: no spike in these chains
        volume=1 if last else 0,
        quote_time=datetime.fromtimestamp(now - quote_age, ET) if quote_age is not None else None,
        last_trade=last_at,
        ask_size=ask_size,
        bid_size=2,
    )


def chain(
    now: float, contracts: list[Contract], stock_age: float = 5.0, source: str = "Alpaca test"
) -> Chain:
    ch = Chain(symbol="BIIB", price=S, prev_close=225.71, change_pct=3.88, iv30=0.30, source=source)
    ch.contracts = contracts
    ch.stock_source, ch.stock_time = "alpaca", datetime.fromtimestamp(now - stock_age, ET)
    return ch


def neighbors(now: float, exp: date, skip: tuple[float, ...] = ()) -> list[Contract]:
    return [k(now, exp, s) for s in (230, 235, 240, 245, 250, 255, 260) if s not in skip]


def kinds(rows) -> list[tuple[str, str]]:
    return [(r.kind, r.contract.symbol[-9:]) for r in rows]


# --------------------------------------------------------------------------- pricing helpers


def test_implied_vol_round_trips_and_refuses_impossible_prices():
    t = 4 / 365
    for cp, strike in (("C", 250.0), ("P", 220.0), ("C", 230.0)):
        p = bs_price(S, strike, t, 0.55, cp)
        assert implied_vol(p, S, strike, t, cp) == pytest.approx(0.55, abs=1e-3)
    assert implied_vol(1.0, S, 200.0, t, "C") is None  # below intrinsic ($34.47)
    assert implied_vol(0.0, S, 250.0, t, "C") is None


def test_years_left_counts_hours_to_the_close():
    exp = date(2026, 10, 16)
    now = datetime(2026, 10, 12, 11, 30, tzinfo=ET).timestamp()
    assert years_left(exp, now) == pytest.approx((4 * 24 + 4.5) / (365 * 24), rel=1e-6)
    assert years_left(exp, datetime(2026, 10, 16, 15, 59, tzinfo=ET).timestamp()) == pytest.approx(
        1 / (365 * 24)
    )


# --------------------------------------------------------------------------- arbitrage


def test_strike_order_arbitrage_on_calls_and_puts():
    now, today, exp = setup()
    lo = k(now, exp, 247.5, bid=0.70, ask=0.80)
    hi = k(now, exp, 252.5, bid=0.90, ask=1.00)  # the higher strike bid above the lower one's ask
    put_lo = k(now, exp, 220.0, "P", bid=1.20, ask=1.30)
    put_hi = k(now, exp, 225.0, "P", bid=0.95, ask=1.05)  # the higher-strike put offered below
    rows = OptionsRadar(ocfg()).misprint_rows(chain(now, [lo, hi, put_lo, put_hi]), today, now)
    v = [r for r in rows if r.kind == "vertical"]
    assert {(r.contract.strike, r.other.strike) for r in v} == {(247.5, 252.5), (225.0, 220.0)}
    call = next(r for r in v if r.contract.cp == "C")
    assert (call.price, call.other_price) == (0.80, 0.90) and call.edge == pytest.approx(0.10)


def test_calendar_and_intrinsic_arbitrage():
    now, today, exp = setup()
    near = k(now, exp, 240.0, bid=1.50, ask=1.60)
    far = k(now, exp + timedelta(days=7), 240.0, bid=1.20, ask=1.30)  # later expiry offered below
    deep = k(now, exp, 220.0, bid=13.8, ask=14.0)  # intrinsic $14.47 at the live price
    rows = OptionsRadar(ocfg()).misprint_rows(chain(now, [near, far, deep]), today, now)
    cal = next(r for r in rows if r.kind == "calendar")
    assert cal.contract is far and cal.other is near and cal.edge == pytest.approx(0.20)
    intr = next(r for r in rows if r.kind == "intrinsic")
    assert intr.fair == pytest.approx(14.47) and intr.edge == pytest.approx(0.47)


def test_arbitrage_needs_live_two_sided_quotes_and_a_live_stock():
    now, today, exp = setup()
    lo = k(now, exp, 247.5, bid=0.70, ask=0.80, quote_age=600)  # a 10-minute-old quote
    hi = k(now, exp, 252.5, bid=0.90, ask=1.00)
    assert OptionsRadar(ocfg()).misprint_rows(chain(now, [lo, hi]), today, now) == []
    cboe = [
        k(now, exp, 247.5, bid=0.70, ask=0.80, quote_age=None),
        k(now, exp, 252.5, bid=0.9, ask=1.0, quote_age=None),
    ]
    assert OptionsRadar(ocfg()).misprint_rows(chain(now, cboe), today, now) == []  # delayed feed
    deep = k(now, exp, 220.0, bid=13.8, ask=14.0)
    assert OptionsRadar(ocfg()).misprint_rows(chain(now, [deep], stock_age=900), today, now) == []


# --------------------------------------------------------------------------- value


def test_an_ask_far_below_its_neighbors_is_a_misprint_to_buy():
    now, today, exp = setup()
    stub = k(now, exp, 252.5, bid=0.0, ask=0.05)  # a stub offer between $250 and $255
    rows = OptionsRadar(ocfg()).misprint_rows(chain(now, [*neighbors(now, exp), stub]), today, now)
    (r,) = [r for r in rows if r.contract is stub]
    fair = bs_price(S, 252.5, years_left(exp, now), IV, "C")
    assert r.kind == "cheap_ask" and r.fair == pytest.approx(fair, rel=0.03)
    assert "neighboring strikes ($250 and $255: 55%)" in r.ref and r.edge == pytest.approx(
        fair - 0.05, rel=0.05
    )


def test_a_bid_far_above_value_is_a_misprint_to_sell():
    now, today, exp = setup()
    rich = k(now, exp, 265.0, bid=0.60, ask=0.90)  # worth ~$0.10 on the curve
    rows = OptionsRadar(ocfg()).misprint_rows(chain(now, [*neighbors(now, exp), rich]), today, now)
    (r,) = [r for r in rows if r.contract is rich]
    assert r.kind == "rich_bid" and r.price == 0.60 and r.fair < 0.30


def test_no_neighbors_no_value_claim():
    """A company-wide IV misses the skew and the day's repricing: without tight neighbors there
    is no fair value, so no value misprint (arbitrage still counts, it needs no model)."""
    now, today, exp = setup()
    stub = k(now, exp, 252.5, bid=0.0, ask=0.05)
    wide = [k(now, exp, s, spread=1.2) for s in (245, 250, 255, 260)]  # every neighbor too wide
    assert OptionsRadar(ocfg()).misprint_rows(chain(now, [*wide, stub]), today, now) == []
    ok = k(now, exp, 252.5, bid=0.40, ask=0.50)  # fairly offered: nothing
    assert OptionsRadar(ocfg()).misprint_rows(chain(now, [*neighbors(now, exp), ok]), today, now) == []


# --------------------------------------------------------------------------- prints


def test_the_biib_twenty_cent_print():
    now, today, exp = setup()
    at = datetime.fromtimestamp(now - 45 * 60, ET)  # 10:45 ET
    biib = k(now, exp, 252.5, bid=0.15, ask=1.25, last=0.20, last_at=at)  # $0.15 / $1.25: no makers
    rows = OptionsRadar(ocfg()).misprint_rows(chain(now, [*neighbors(now, exp), biib]), today, now)
    (r,) = [r for r in rows if r.contract is biib]
    assert r.kind == "print" and not r.live and r.price == 0.20 and r.fair > 0.5


def test_prints_that_are_not_misprints():
    now, today, exp = setup()
    at = datetime.fromtimestamp(now - 10 * 60, ET)
    fair = bs_price(S, 262.5, years_left(exp, now), IV, "C")
    tight = k(
        now,
        exp,
        262.5,
        bid=round(fair - 0.02, 2),
        ask=round(fair + 0.02, 2),
        last=round(fair * 0.6, 2),
        last_at=at,
    )
    old = k(now, exp, 257.5, bid=0.0, ask=1.0, last=0.02, last_at=datetime.fromtimestamp(now - 3 * 3600, ET))
    high = k(now, exp, 252.5, bid=0.0, ask=9.0, last=3.0, last_at=at)  # far above value: a spike, not this
    rows = OptionsRadar(ocfg()).misprint_rows(
        chain(now, [*neighbors(now, exp), tight, old, high]), today, now
    )
    assert [r for r in rows if r.contract in (tight, old, high)] == []  # tight: the market says 60% is fine


# --------------------------------------------------------------------------- alerts


def test_misprint_hits_dedupe_push_on_their_own_budget_and_say_why():
    now, today, exp = setup()
    radar = OptionsRadar(ocfg(misprint_daily_pushes=1))
    lo = k(now, exp, 247.5, bid=0.70, ask=0.80)
    hi = k(now, exp, 252.5, bid=0.90, ask=1.00)
    biib = Listing("BIIB", "Biogen", market_cap=33e9)
    (hit,) = radar.scan(chain(now, [lo, hi], source="Alpaca indicative"), biib, now)
    assert hit.kind == "misprint" and hit.severity == "HIGH" and hit.budget == "misprint"
    assert radar.pushes.get(radar._day(now), 0) == 0  # the spike/flow budget is untouched
    assert any("indicative quotes" in x for x in hit.reasons)
    assert radar.scan(chain(now, [lo, hi]), biib, now + 5) == []  # already shown today
    lo2 = k(now, exp, 247.5, bid=0.50, ask=0.60)  # the edge doubled to $0.30: shown again
    (again,) = radar.scan(chain(now, [lo2, hi]), biib, now + 10)
    assert again.severity == "MEDIUM" and "push limit for misprints" in again.reasons[0]
    d = again.to_dict()["misprints"][0]
    assert d["kind"] == "vertical" and d["what"] == "strike-order arbitrage" and d["other_price"] == 0.90


async def test_engine_misprint_alerts_read_like_trades(tmp_path):
    eng, cap = engine(tmp_path)
    now, today, exp = setup()

    def xyz(c: Contract) -> Contract:
        c.symbol, c.underlying = "XYZ" + c.symbol[4:], "XYZ"
        return c

    stub = xyz(k(now, exp, 252.5, bid=0.0, ask=0.05))
    lo, hi = xyz(k(now, exp, 247.5, bid=0.70, ask=0.80)), xyz(k(now, exp, 252.5, bid=0.90, ask=1.00))
    ch = chain(now, [*[xyz(c) for c in neighbors(now, exp, skip=(250,))], stub])
    ch.symbol = "XYZ"
    hits = eng.options_radar.scan(ch, eng.universe.get("XYZ"), now)
    ch2 = chain(now, [lo, hi])
    ch2.symbol = "XYZ"
    eng.options_radar.misprint_seen.clear()
    hits += eng.options_radar.scan(ch2, eng.universe.get("XYZ"), now)
    await eng.on_options([h for h in hits if h.kind == "misprint"])
    await asyncio.sleep(0.05)
    titles = [a.title for a in cap.alerts]
    assert any(
        t.startswith("🎯 Misprint: XYZ $252.5 call") and "offered at $0.05, worth ~$" in t for t in titles
    )
    arb = next(a for a in cap.alerts if a.title.startswith("🎯 Arbitrage"))
    assert (
        "buy XYZ $247.5 call" in arb.title
        and "sell XYZ $252.5 call" in arb.title
        and "(+$0.10 a share)" in arb.title
    )
    assert "$10 a contract) locked in now" in arb.body and "Misprints vanish in seconds" in arb.body
    cheap = next(a for a in cap.alerts if a.title.startswith("🎯 Misprint"))
    assert "(3 offered)" in cheap.body and "Data: quote 2s old" in cheap.body


# --------------------------------------------------------------------------- the stream


async def test_a_stub_offer_on_the_stream_is_judged_at_once():
    now, today, exp = setup(time.time())
    cfg = ocfg(alpaca_key="PK", alpaca_secret="SK", provider="alpaca", fallback=False)
    u = uni(li("BIIB", 33e9, 3.9))
    f = OptionsFeed(cfg, None, u, OptionsRadar(cfg))  # type: ignore[arg-type]
    assert f.stream is not None and f.stream.on_misprint is not None
    target = k(now, exp, 252.5, bid=0.40, ask=0.50)
    ch = chain(now, [*neighbors(now, exp), target])
    f.live.update("BIIB", S, 225.71, now, "alpaca")
    f.prioritize(["BIIB"])
    f._watch(ch, now, False)
    w = f.stream.watch.get(target.symbol)
    assert w is not None and w.fair == pytest.approx(
        bs_price(S, 252.5, years_left(exp, now), IV, "C"), rel=0.05
    )
    got: list = []

    async def on_hits(hits: list) -> None:
        got.extend(hits)

    f._on_hits = on_hits
    f.stream.handle([{"T": "q", "S": target.symbol, "bp": 0.0, "ap": 0.05, "t": time.time()}])
    await asyncio.sleep(0.05)
    (hit,) = got
    assert hit.kind == "misprint" and hit.misprints[0].kind == "cheap_ask" and hit.misprints[0].price == 0.05
    assert "caught on the live websocket quote" in hit.reasons and f.stream.misprint_trips == 1
    f.stream.handle([{"T": "q", "S": target.symbol, "bp": 0.0, "ap": 0.04, "t": time.time()}])
    await asyncio.sleep(0.02)
    assert len(got) == 1  # once a minute per contract
    assert f.status()["live"]["stream_misprints"] == 1


def test_stream_misprint_check_needs_a_fair_value_and_a_real_gap():
    calls: list[Any] = []
    st = OptionStream("wss://x", "k", "s", lambda *a: None, on_misprint=lambda *a: calls.append(a))
    st.set_candidates("X", [Watch("X1", "X", 0.30, fair=0.50), Watch("X2", "X", 0.30)])
    now = time.time()
    st.handle([{"T": "q", "S": "X1", "bp": 0.40, "ap": 0.55, "t": now}], now)  # fair market
    st.handle([{"T": "q", "S": "X2", "bp": 0.0, "ap": 0.01, "t": now}], now)  # no fair value known
    assert calls == []
    st.handle([{"T": "q", "S": "X1", "bp": 1.10, "ap": 1.30, "t": now}], now)  # bid 2.2x fair
    assert len(calls) == 1 and calls[0][1] == 1.10
