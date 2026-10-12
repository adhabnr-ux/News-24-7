"""Options blowing up (OptionsRadar._blowup_hits) and honest bases, replayed from Oct 12 2026.

* LITE fell 9.3% ($1,103.36 Friday to ~$1,000) and its Oct 16 puts went 4-13x. Our alert said
  "$925 put +36% (unconfirmed)": it used Friday's stock price for today and Thursday's for
  yesterday, and valued yesterday's contract at today's crash-inflated implied vol, so a real
  $0.55 close looked like a stale print of a $5.30 contract.
* FLEX sat -1.9% while its whole November chain was repriced from ~45% to ~90% implied vol:
  calls worth a cent bid $0.20-$0.65 (a broker showed +4,400% to +8,400%). Valuing yesterday's
  one-cent closes at today's 90% made every one of them look stale.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime

import pytest

from news247.market.options import Chain, Contract, OptionsRadar, bs_price, prev_session
from news247.market.universe import Listing

from .test_options import ET, engine, ocfg

NOW = datetime(2026, 10, 12, 11, 30, tzinfo=ET).timestamp()
TODAY = date(2026, 10, 12)
FRI = date(2026, 10, 9)
OCT16, OCT23, NOV20 = date(2026, 10, 16), date(2026, 10, 23), date(2026, 11, 20)


def yday_value(s_prev: float, k: float, exp: date, iv: float, cp: str) -> float:
    return bs_price(s_prev, k, (exp - FRI).days / 365, iv, cp)


def con(sym: str, exp: date, cp: str, k: float, *, prev: float, bid: float | None = None, ask: float | None = None,
        last: float | None = None, vol: int = 0, prev_date: date = FRI, quote_age: float = 3.0) -> Contract:  # fmt: skip
    occ = f"{sym}{exp:%y%m%d}{cp}{int(k * 1000):08d}"
    return Contract(
        occ, sym, exp, cp, k,
        bid=bid, ask=ask if ask is not None else (round(bid * 1.15, 2) if bid else None),
        last=last, prev_close=round(prev, 2) if prev >= 0.01 else prev, volume=vol,
        last_trade=datetime(2026, 10, 12, 11, 25, tzinfo=ET) if last else None,
        quote_time=datetime.fromtimestamp(NOW - quote_age, ET) if bid is not None else None,
        prev_close_date=prev_date, iv=None,
    )  # fmt: skip


def lite_chain() -> Chain:
    s_prev, s_now, iv_prev = 1103.355, 1000.5, 0.70
    cs = []
    for k in (1050, 1080, 1100, 1120, 1150):  # near the money yesterday: they carry yesterday's IV
        for cp in ("C", "P"):
            cs.append(con("LITE", OCT23, cp, k, prev=yday_value(s_prev, k, OCT23, iv_prev, cp)))
    for k, bid in ((945, 9.2), (940, 8.2), (935, 7.6), (930, 7.0), (927.5, 5.8)):
        cs.append(con("LITE", OCT16, "P", k, prev=yday_value(s_prev, k, OCT16, iv_prev, "P"), bid=bid))
    cs.append(con("LITE", OCT16, "P", 925, prev=0.55, bid=7.08, ask=9.01, last=7.21, vol=100))
    ch = Chain("LITE", price=s_now, prev_close=s_prev, change_pct=(s_now / s_prev - 1) * 100, iv30=1.10,
               source="Alpaca indicative", contracts=cs)  # fmt: skip
    ch.stock_source, ch.stock_time = "alpaca", datetime.fromtimestamp(NOW - 5, ET)
    return ch


def flex_chain() -> Chain:
    s_prev, s_now, iv_prev, iv_now = 119.83, 117.6, 0.45, 0.90
    cs = []
    for k in (110, 115, 120, 125, 130):
        for cp in ("C", "P"):
            cs.append(con("FLEX", NOV20, cp, k, prev=yday_value(s_prev, k, NOV20, iv_prev, cp)))
    for k, prev in ((150, 0.06), (160, 0.03), (170, 0.0125), (180, 0.0127), (185, 0.015), (190, 0.0155)):
        now_v = bs_price(s_now, k, (NOV20 - TODAY).days / 365, iv_now, "C")
        cs.append(con("FLEX", NOV20, "C", k, prev=prev, bid=round(now_v * 0.8, 2)))
    ch = Chain("FLEX", price=s_now, prev_close=s_prev, change_pct=(s_now / s_prev - 1) * 100, iv30=iv_now,
               source="Alpaca indicative", contracts=cs)  # fmt: skip
    ch.stock_source, ch.stock_time = "alpaca", datetime.fromtimestamp(NOW - 5, ET)
    return ch


def test_yesterdays_iv_comes_from_yesterdays_closes():
    r = OptionsRadar(ocfg())
    assert r.yesterday_iv(lite_chain(), TODAY) == pytest.approx(0.70, abs=0.01)
    assert r.yesterday_iv(flex_chain(), TODAY) == pytest.approx(0.45, abs=0.01)
    bare = Chain("X", price=10, prev_close=10, contracts=[con("X", OCT23, "C", 10, prev=0.5)])
    assert r.yesterday_iv(bare, TODAY) is None  # one contract is not a curve


def test_the_lite_925_put_is_a_real_spike_not_plus_36():
    radar = OptionsRadar(ocfg())
    hits = radar.scan(lite_chain(), Listing("LITE", "Lumentum Holdings", market_cap=90e9), NOW)
    spike = next(h for h in hits if h.kind == "spike")
    top = spike.spikes[0]
    assert top.contract.strike == 925 and not top.stale_base
    assert top.move_pct == pytest.approx(1210.9, abs=0.1) and top.real  # a real +1,211%, bid-confirmed
    assert radar.pushes[radar._day(NOW)] == 1  # one push for LITE: its blowup
    # yesterday's value is priced at yesterday's 70%, not today's 110%
    assert top.fair_prev == pytest.approx(yday_value(1103.355, 925, OCT16, 0.70, "P"), rel=0.05)


def test_lite_puts_exploding():
    radar = OptionsRadar(ocfg())
    hits = radar.scan(lite_chain(), Listing("LITE", "Lumentum Holdings", market_cap=90e9), NOW)
    (b,) = [h for h in hits if h.kind == "blowup"]
    assert b.side == "puts" and b.count == 6 and b.blowups[0].multiple >= 10
    assert {r.contract.strike for r in b.blowups} <= {925, 927.5, 930, 935, 940, 945}
    assert b.severity == "HIGH"  # the blowup carries the push ...
    spike = next(h for h in hits if h.kind == "spike")
    assert spike.severity == "MEDIUM" and "pushed as part of this company's blowup alert" in spike.reasons
    assert radar.scan(lite_chain(), None, NOW + 60) == []  # shown once, until it doubles


def test_flex_repriced_with_the_stock_flat():
    radar = OptionsRadar(ocfg())
    (b,) = [
        h
        for h in radar.scan(flex_chain(), Listing("FLEX", "Flex Ltd.", market_cap=43e9), NOW)
        if h.kind == "blowup"
    ]
    assert b.side == "calls" and b.count >= 5 and b.severity == "HIGH"
    assert b.iv_prev == pytest.approx(0.45, abs=0.01) and b.iv_now == 0.90
    far = next(r for r in b.blowups if r.contract.strike == 170)
    # a flat 45% vol values the 42%-out call at ~$0.09 yesterday (the market, with its skew, said
    # $0.0125): the base errs high, so the multiple is understated, never inflated, and still 19x
    assert far.base >= 0.0125 and far.multiple > 15


def test_a_close_from_another_day_is_replaced_by_yesterdays_value():
    radar = OptionsRadar(ocfg())
    ch = lite_chain()
    old = con("LITE", OCT16, "P", 920, prev=3.07, prev_date=date(2026, 10, 6), bid=5.0)
    fair = radar.fair_prev(old, ch, TODAY)
    base, replaced = radar.honest_base(old, fair, TODAY)
    assert replaced and base == pytest.approx(yday_value(1103.355, 920, OCT16, 0.70, "P"), rel=0.05)
    assert prev_session(TODAY) == FRI


def test_calm_chains_do_not_blow_up():
    ch = flex_chain()
    for c in ch.contracts:
        if c.bid is not None:
            c.bid = round(c.prev_close * 1.5, 2) or 0.01  # up 50%: ordinary
    assert [h for h in OptionsRadar(ocfg()).scan(ch, None, NOW) if h.kind == "blowup"] == []


async def test_engine_blowup_alerts(tmp_path):
    eng, cap = engine(tmp_path)
    ch = flex_chain()
    ch.symbol = "XYZ"
    for c in ch.contracts:
        c.underlying = "XYZ"
        c.symbol = "XYZ" + c.symbol[4:]
    hits = [h for h in eng.options_radar.scan(ch, eng.universe.get("XYZ"), NOW) if h.kind == "blowup"]
    await eng.on_options(hits)
    await asyncio.sleep(0.05)
    a = cap.alerts[0]
    assert a.title.startswith("🧨 XYZ calls repriced:") and "implied vol 45% → 90%" in a.title
    assert "the market is pricing an event" in a.body and "Implied vol: 45% at yesterday's close" in a.body
    assert "XYZ $180 call (Nov 20): $0.01 → $" in a.body and "live bid" in a.body
    assert a.severity.name == "HIGH" and "…and 1 more calls up 4x+." in a.body


def test_blowup_state_survives_a_restart(tmp_path):
    p = tmp_path / "s.json"
    r = OptionsRadar(ocfg(), state_path=p)
    assert [h for h in r.scan(flex_chain(), None, NOW) if h.kind == "blowup"]
    r2 = OptionsRadar(ocfg(), state_path=p)
    r2._day = lambda now: datetime.fromtimestamp(NOW, ET).date().isoformat()  # type: ignore[method-assign]
    assert r2.state["FLEX"].blowup_calls >= 5
