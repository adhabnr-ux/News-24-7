"""After the alert (market/follow.py, the follow lane in market/options.py, the engine's text).

The case: Oct 12 2026, the META $785 call (Oct 14). Friday's close $0.18; it ran to $1.27 by
9:45 ET, sank to $0.62 with the stock holding, ran to $1.83 by 11:20, and traded $1.05-$1.30
when the alert went out at 12:51 ET. The question was: "how am I supposed to know if it keeps
going higher or stops out, assuming I entered late?" These tests replay that real tape.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from news247.http import HttpClient
from news247.market.follow import (
    FADING,
    HOLDING,
    RUNNING,
    STOPPED,
    FollowDesk,
    lead_contracts,
    option_path,
    parse_option_bars,
    play_card,
)
from news247.market.live import LiveQuotes, OptionStream, parse_stock_ranges
from news247.market.options import BlowupRow, Chain, Contract, OptionsFeed, OptionsHit, OptionsRadar
from news247.models import Severity

from .conftest import Recorder
from .test_options import ET, alpaca_cfg, engine, li, ocfg, uni
from .test_options_live import utc

# META Oct 14 $785 call, Oct 12 2026, 5-minute bars (epoch, open, high, low, close, volume).
# Friday's close: $0.18. Source: OPRA via TradingView, fetched that afternoon.
CALL = [
    (1791811800, 0.23, 0.6, 0.23, 0.37, 40),
    (1791812100, 0.45, 0.88, 0.43, 0.79, 149),
    (1791812400, 0.81, 1.11, 0.79, 1.03, 97),
    (1791812700, 0.99, 1.27, 0.91, 1.0, 78),
    (1791813000, 1.15, 1.17, 0.9, 0.95, 61),
    (1791813300, 0.9, 0.93, 0.66, 0.66, 40),
    (1791813600, 0.7, 0.8, 0.62, 0.75, 85),
    (1791813900, 0.74, 0.79, 0.66, 0.67, 41),
    (1791814200, 0.66, 0.72, 0.63, 0.63, 15),
    (1791814500, 0.63, 0.74, 0.63, 0.73, 17),
    (1791814800, 0.73, 0.75, 0.69, 0.71, 18),
    (1791815100, 0.67, 0.7, 0.65, 0.69, 7),
    (1791815400, 0.8, 0.88, 0.75, 0.75, 55),
    (1791815700, 0.79, 0.79, 0.6, 0.65, 22),
    (1791816000, 0.74, 0.77, 0.72, 0.74, 15),
    (1791816300, 0.75, 1.4, 0.7, 1.21, 246),
    (1791816600, 1.21, 1.21, 0.84, 0.99, 26),
    (1791816900, 0.95, 1.0, 0.88, 0.92, 14),
    (1791817200, 0.88, 1.2, 0.83, 1.17, 43),
    (1791817500, 1.15, 1.43, 1.15, 1.38, 96),
    (1791817800, 1.34, 1.35, 1.19, 1.3, 14),
    (1791818100, 1.36, 1.78, 1.32, 1.67, 82),
    (1791818400, 1.73, 1.83, 1.09, 1.19, 127),
    (1791818700, 1.28, 1.32, 1.16, 1.16, 10),
    (1791819000, 1.33, 1.36, 1.3, 1.35, 38),
    (1791819300, 1.39, 1.39, 1.2, 1.2, 46),
    (1791819600, 1.07, 1.07, 1.05, 1.05, 6),
    (1791819900, 1.12, 1.25, 1.1, 1.25, 17),
    (1791820200, 1.27, 1.3, 1.15, 1.18, 6),
    (1791820500, 1.17, 1.18, 1.15, 1.17, 6),
    (1791820800, 1.14, 1.14, 1.14, 1.14, 1),
    (1791821100, 1.1, 1.1, 0.99, 0.99, 9),
    (1791821400, 0.98, 1.05, 0.98, 0.98, 3),
    (1791821700, 1.05, 1.15, 1.0, 1.15, 18),
    (1791822000, 1.15, 1.15, 1.08, 1.08, 5),
    (1791822300, 1.07, 1.07, 1.05, 1.05, 6),
    (1791822600, 1.07, 1.1, 1.06, 1.09, 16),
    (1791823200, 1.07, 1.3, 1.07, 1.3, 26),
]
# META, Oct 12 2026, 5-minute bars (epoch, high, low, close). Friday's close: $718.67.
STOCK = [
    (1791811800, 740.45, 727.64, 734.08),
    (1791812100, 743.16, 733.775, 741.9351),
    (1791812400, 746.6, 741.12, 745.205),
    (1791812700, 748.1563, 744.1, 745.32),
    (1791813000, 748.15, 744.233, 746),
    (1791813300, 745.9699, 740.28, 742.29),
    (1791813600, 743.99, 739, 743.68),
    (1791813900, 744.76, 742.27, 742.62),
    (1791814200, 743.497, 741.36, 742.3),
    (1791814500, 744.4499, 742.33, 744.3),
    (1791814800, 744.45, 742.89, 743.395),
    (1791815100, 745.22, 742.9, 745.135),
    (1791815400, 746.52, 743.74, 744.46),
    (1791815700, 744.65, 741.2601, 742.84),
    (1791816000, 745.5, 742.74, 745.01),
    (1791816300, 749.59, 743.68, 746.455),
    (1791816600, 746.92, 741.77, 744.09),
    (1791816900, 746.08, 743.8218, 744.61),
    (1791817200, 748.3, 744.1701, 746.56),
    (1791817500, 747.49, 745.7, 746.62),
    (1791817800, 748.0999, 746.1505, 747.51),
    (1791818100, 751.58, 747.43, 750.7266),
    (1791818400, 751.2525, 743.72, 745.8),
    (1791818700, 747, 745.1825, 745.6434),
    (1791819000, 748.2753, 745.45, 747.48),
    (1791819300, 748.085, 745, 745.43),
    (1791819600, 746.2999, 743.29, 745.105),
    (1791819900, 747.55, 744.77, 747.46),
    (1791820200, 747.855, 745.47, 745.8806),
    (1791820500, 747.36, 745.78, 746.91),
    (1791820800, 747.01, 745.81, 746.08),
    (1791821100, 746.22, 744.3978, 744.785),
    (1791821400, 745.28, 744.43, 744.845),
    (1791821700, 746.7, 744.73, 746.245),
    (1791822000, 746.9695, 744.915, 744.915),
    (1791822300, 745.1999, 743.83, 744.7201),
    (1791822600, 745.86, 744.82, 745.6651),
    (1791822900, 746, 744.555, 745.5),
    (1791823200, 746.8, 745.1, 745.32),
    (1791823500, 747.97, 745.27, 747.2901),
    (1791823800, 748, 745.67, 746.825),
    (1791824100, 749.1591, 741.1, 743.295),
]

META_PREV = 718.67
OCC = "META261014C00785000"
EXPIRY = date(2026, 10, 14)
DAY = datetime(2026, 10, 12, tzinfo=ET)


@pytest.fixture(autouse=True)
def _no_alpaca_env(monkeypatch):
    for name in ("ALPACA_KEY", "ALPACA_SECRET", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)


def at(hh: int, mm: int, ss: int = 0) -> float:
    return DAY.replace(hour=hh, minute=mm, second=ss).timestamp()


def stock_at(ts: float) -> float:
    """META's 5-minute close for the bar holding ``ts``."""
    return max((r for r in STOCK if r[0] <= ts), key=lambda r: r[0])[3]


def meta_contract(bid: float, ask: float, ts: float, prev: float = 0.18) -> Contract:
    """The META call; on the wall clock (tests that need it) it expires two days after ``ts``."""
    expiry = (
        EXPIRY if ts < at(16, 0) + 2 * 86400 else datetime.fromtimestamp(ts, ET).date() + timedelta(days=2)
    )
    return Contract(
        symbol=OCC,
        underlying="META",
        expiry=expiry,
        cp="C",
        strike=785.0,
        bid=bid,
        ask=ask,
        last=round((bid + ask) / 2, 2),
        prev_close=prev,
        volume=900,
        iv=0.5,
        quote_time=datetime.fromtimestamp(ts, ET),
    )


def meta_chain(c: Contract, price: float) -> Chain:
    ch = Chain(
        "META", price=price, prev_close=META_PREV, change_pct=(price / META_PREV - 1) * 100, source="t"
    )
    ch.contracts.append(c)
    ch.quote_time = c.quote_time
    return ch


def meta_hit(c: Contract, price: float, ts: float) -> OptionsHit:
    ch = meta_chain(c, price)
    row = BlowupRow(c, 0.18, c.bid or 0.0, (c.bid or 0.0) / 0.18, "live bid")
    return OptionsHit("META", "blowup", "HIGH", ch, None, blowups=[row], side="calls", count=6, detected=ts)


def replay(desk: FollowDesk, after: float) -> list[tuple[str, str, float]]:
    """Each 5-minute bar after ``after`` as two quotes 30s apart: the close as the mid with a $0.06
    spread (the bars have no bid/ask), the stock at its close. -> [(HH:MM, event, bid)]."""
    out = []
    for t, _o, _h, _l, c, _v in CALL:
        if t < after:
            continue
        for dt in (240, 270):
            ts = t + dt
            for e in desk.observe(OCC, round(c - 0.03, 2), round(c + 0.03, 2), ts, stock_at(ts), None, ts):
                out.append((datetime.fromtimestamp(ts, ET).strftime("%H:%M"), e.kind, e.f.bid))
    return out


# --------------------------------------------------------------------------- the stock's day


def test_live_quotes_keep_the_days_path_and_see_the_meta_jump_at_the_open():
    lq = LiveQuotes()
    for t, _h, _l, c in STOCK[:3]:  # 9:30, 9:35, 9:40 closes
        lq.update("META", c, META_PREV, t + 299, "alpaca")
    p = lq.path("META", at(9, 45))
    assert p is not None and p.high == 745.205 and p.low == 734.08
    pct, frm, frm_ts = lq.jump("META", 600, at(9, 45))  # type: ignore[misc]
    assert frm == 734.08 and pct == pytest.approx(1.516, abs=0.01)  # +1.5% in 10 minutes
    assert lq.jump("META", 600, at(10, 30)) is None  # its last price is 50 minutes old
    lq.update("META", 742.29, META_PREV, at(9, 59, 59), "alpaca")
    assert lq.jump("META", 600, at(10, 0)) is None  # one price in the window: no move to measure
    lq.update("META", 746.0, META_PREV, at(9, 54, 59), "alpaca")  # an older reading arrives late
    assert lq.get("META", 180, at(10, 0)).price == 742.29  # type: ignore[union-attr]  # ... and is ignored
    lq.update("META", 748.0, META_PREV, at(10, 3), "alpaca")
    lq.update("META", 742.0, META_PREV, at(10, 6), "alpaca")
    pct, frm, _ = lq.jump("META", 600, at(10, 6))  # type: ignore[misc]
    assert pct == pytest.approx(-0.80, abs=0.01) and frm == 748.0  # a drop is measured from the high
    lq.set_range("META", 748.16, 727.64, 728.495, at(10, 0))  # the day's bar widens the range
    p = lq.path("META", at(10, 0))
    assert p is not None and (p.high, p.low, p.open, p.high_ts) == (748.16, 727.64, 728.495, 0.0)
    assert lq.path("META", at(10, 0) + 86400) is None  # tomorrow is a new day
    lq.update("META", 750.0, 745.0, at(10, 0) + 86400, "alpaca")
    p = lq.path("META", at(10, 0) + 86400)
    assert p is not None and p.high == 750.0 and len(p.hist) == 1


def test_seeding_the_morning_and_the_history_window():
    lq = LiveQuotes()
    lq.seed("meta", [(t + 299, c) for t, _h, _l, c in STOCK])
    p = lq.path("META", at(13, 0))
    assert p is not None and p.high == 750.7266 and p.low == 734.08  # closes seen: 11:15 and 9:30
    assert p.hist[0][0] >= p.hist[-1][0] - 1800  # only the last 30 minutes are kept
    assert lq.quotes == {}  # seeding is history, not a live quote
    lq.seed("", [])  # nothing to do
    lq.update("FLAT", 100.0, 100.0, at(10, 0), "y")
    lq.update("FLAT", 100.05, 100.0, at(10, 5), "y")
    pct, _, _ = lq.jump("FLAT", 600, at(10, 5))  # type: ignore[misc]
    assert abs(pct) < 0.1
    assert lq.jump("NONE", 600, at(10, 5)) is None


def test_alpaca_day_ranges_only_from_todays_bar():
    today = date(2026, 10, 12)
    doc = {
        "META": {"dailyBar": {"t": utc(DAY), "h": 751.58, "l": 727.64, "o": 728.495}},
        "OLD": {"dailyBar": {"t": utc(DAY - timedelta(days=3)), "h": 1, "l": 1, "o": 1}},
        "BAD": "x",
    }
    assert parse_stock_ranges(doc, today) == {"META": (751.58, 727.64, 728.495)}
    assert parse_stock_ranges({"snapshots": doc}, today)["META"][0] == 751.58
    assert parse_stock_ranges(None, today) == {}


# --------------------------------------------------------------------------- the option's day


def alpaca_bars_doc(rows: list) -> dict:
    return {
        "bars": {
            OCC: [
                {"t": datetime.fromtimestamp(t, ET).isoformat(), "o": o, "h": h, "l": lo, "c": c, "v": v}
                for t, o, h, lo, c, v in rows
            ]
        },
        "next_page_token": None,
    }


def test_the_meta_calls_day_from_its_bars():
    bars = parse_option_bars(alpaca_bars_doc(CALL))[OCC]
    assert len(bars) == len(CALL) and bars[0][0] == at(9, 30)
    p = option_path(bars, 0.18)
    assert p is not None
    assert p.began_ts == at(9, 30)  # the first bar already traded $0.60, 3.3x Friday's close
    assert (p.high, p.high_ts) == (1.83, at(11, 20))
    assert (p.low, p.low_ts, p.open, p.last, p.to_ts) == (0.23, at(9, 30), 0.23, 1.30, at(12, 40))
    assert p.legs == 3  # 9:30-9:45 to $1.27, 10:45 to $1.40, 11:05-11:20 to $1.83
    assert option_path(CALL[:6], 0.18).legs == 1  # type: ignore[union-attr]  # wicks are not legs
    assert p.volume == sum(r[5] for r in CALL)
    assert option_path([], 0.18) is None
    assert parse_option_bars({"bars": {OCC: [{"t": "bad"}, "x", {"t": utc(DAY), "o": 1}]}}) == {}
    assert parse_option_bars(None) == {}


async def test_alpaca_option_bars_request(server: Recorder, http: HttpClient):
    from news247.market.options import AlpacaSource

    server.on("/v1beta1/options/bars", alpaca_bars_doc(CALL[:3]))
    cfg = alpaca_cfg(server)
    src = AlpacaSource(cfg, http)

    async def pace(_s: float) -> None:
        return None

    got = await src.option_bars([OCC], DAY.replace(hour=9, minute=30), pace)
    q = server.requests[-1]["query"]
    assert q["symbols"] == OCC and q["timeframe"] == "5Min" and q["start"].startswith("2026-10-12T09:30")
    assert [r[4] for r in got[OCC]] == [0.37, 0.79, 1.03]


# --------------------------------------------------------------------------- if you buy now


def test_play_card_for_a_late_buyer_at_1258():
    """12:58 ET, the real quote: bid $1.52 / ask $1.69 with META at $746.475."""
    now = at(12, 58)
    c = meta_contract(1.52, 1.69, now)
    ch = meta_chain(c, 746.475)
    lq = LiveQuotes()
    lq.seed("META", [(t + 299, c_) for t, _h, _l, c_ in STOCK])
    lq.set_range("META", 751.58, 727.64, 728.495, now)
    path = option_path([r for r in CALL], 0.18)
    play = play_card(c, ch, 0.18, now, ocfg(), iv_prev=0.35, path=path, stock_path=lq.path("META", now))
    assert play is not None
    assert (play.entry, play.entry_how) == (1.69, "ask")
    assert play.breakeven == pytest.approx(786.69) and play.breakeven_pct == pytest.approx(5.39, abs=0.01)
    assert play.iv is not None and 0.2 < play.iv < 1.5
    assert play.delta is not None and 0.05 < play.delta < 0.3
    assert play.per_1pct is not None and play.per_1pct > 0
    cases = {what: pct for what, _v, pct in play.scenarios}
    assert cases["META 1% higher now"] > 0
    assert cases["META flat into the close"] < 0  # three hours of decay on a two-day option
    assert cases["META gives back half today's move"] < -60  # the stock is the whole trade
    assert any(k.startswith("implied vol back to yesterday's 35%") for k in cases)
    assert play.stop == pytest.approx(1.04) and play.fade == pytest.approx(1.28)  # mid $1.605 -35% / -20%
    assert play.stock_fade == pytest.approx(735.13, abs=0.01) and play.stock_stop == 718.67
    assert play.off_high_pct == pytest.approx(-7.65, abs=0.01)
    assert (
        "push #3 of the day: late in the move" in play.verdict
        and "within 8% of the day's $1.83 high" in play.verdict
    )
    d = play.to_dict()
    assert d["scenarios"][0]["case"] == "META 1% higher now" and d["path"]["legs"] == 3


def test_play_card_at_1251_is_a_pullback_and_puts_mirror():
    path = option_path(CALL, 0.18)
    now = at(12, 51)
    c = meta_contract(1.05, 1.15, now)
    play = play_card(c, meta_chain(c, 746.825), 0.18, now, ocfg(), path=path)
    assert play is not None and play.off_high_pct == pytest.approx(-37.2, abs=0.1)
    assert "37% under the day's $1.83 high: a pullback, not the run" in play.verdict
    put = Contract("META261014P00700000", "META", EXPIRY, "P", 700.0, bid=0.20, ask=0.25, prev_close=1.0)
    pp = play_card(put, meta_chain(put, 746.825), 1.0, now, ocfg())
    assert pp is not None and pp.breakeven == pytest.approx(699.75) and pp.stock_stop is None  # META is up
    assert pp.scenarios[0][0] == "META 1% lower now" and pp.delta is not None and pp.delta < 0
    none = Contract("X", "META", EXPIRY, "C", 785.0)
    assert play_card(none, meta_chain(none, 746.0), 0.18, now, ocfg()) is None  # no quote, no price
    blind = meta_contract(1.05, 1.15, now)
    ch = meta_chain(blind, 746.0)
    ch.price = None
    bp = play_card(blind, ch, 0.18, now, ocfg())
    assert bp is not None and bp.delta is None and bp.breakeven_pct is None and bp.scenarios == []


# --------------------------------------------------------------------------- following


def test_replay_a_944_alert_through_the_real_tape():
    """Alerted after the 9:40 bar ($1.03): a late entrant at the $1.06 ask."""
    desk = FollowDesk(ocfg())
    ts = at(9, 44, 59)
    (f,) = desk.track(meta_hit(meta_contract(1.00, 1.06, ts), stock_at(ts), ts), ts)
    assert (f.entry, f.mark, f.high, f.stop, f.fade) == (1.06, 1.03, 1.00, 0.67, 0.82)
    events = replay(desk, at(9, 45))
    assert events == [
        ("09:59", "stopped", 0.63),  # -41% with the stock holding: the opening premium draining
        ("10:49", "rerun", 1.18),  # back above the $1.00 it reached before the stop
        ("10:54", "fading", 0.96),
        ("11:19", "running", 1.64),
        ("11:24", "fading", 1.16),
        ("11:44", "stopped", 1.02),
    ]
    assert f.state == STOPPED and f.stops == 2 and f.reruns == 1 and not f.ended
    assert desk.pushed_today == 6 and len(desk.events) == 6


def test_a_late_alert_near_the_top():
    """Alerted after the 11:05 bar ($1.38, ask $1.41): it ran to a $1.64 bid (+16%, short of a +25%
    step: no push), then the stop trailing that high took it out at $1.02 (-28% on the ask)."""
    desk = FollowDesk(ocfg())
    ts = at(11, 9, 59)
    desk.track(meta_hit(meta_contract(1.35, 1.41, ts), stock_at(ts), ts), ts)
    assert replay(desk, at(11, 10)) == [("11:24", "fading", 1.16), ("11:44", "stopped", 1.02)]
    f = desk.items[OCC]
    assert f.high == 1.64 and f.high_ts == at(11, 19, 30) and f.stop == pytest.approx(1.07)


def test_one_pulled_bid_is_not_a_stop_and_the_spread_is_not_a_fade():
    desk = FollowDesk(ocfg())
    ts = at(12, 51)
    desk.track(meta_hit(meta_contract(1.05, 1.15, ts), 746.8, ts), ts)  # mid $1.10
    assert desk.observe(OCC, 0.95, 1.15, ts + 1, 746.8) == []  # the spread alone: holding
    assert desk.items[OCC].state == HOLDING
    assert desk.observe(OCC, 0.50, 1.15, ts + 5, 746.8) == []  # a pulled bid, once ...
    assert desk.observe(OCC, 1.04, 1.12, ts + 30, 746.8) == []  # ... gone a moment later
    assert desk.items[OCC].state == HOLDING
    assert desk.observe(OCC, 0.80, 1.00, ts + 40, 746.0) == []  # a real drop: seen once
    assert desk.observe(OCC, 0.81, 1.00, ts + 45, 746.0) == []  # too soon to confirm
    (e,) = desk.observe(OCC, 0.79, 0.99, ts + 61, 746.0)  # 21s later: confirmed
    assert e.kind == "fading" and e.severity == "HIGH" and "28% under the $1.10 mid when alerted" in e.why
    assert desk.observe(OCC, None, 0.99, ts + 70, 746.0) == []  # no bid: nothing to judge
    assert desk.observe(OCC, 0.70, 0.99, ts - 100, 746.0) == []  # an older quote than the last


def test_stock_rules_fade_and_stop_and_a_milder_second_quote_wins():
    desk = FollowDesk(ocfg())
    ts = at(12, 51)
    desk.track(meta_hit(meta_contract(1.05, 1.15, ts), 746.8, ts), ts)
    f = desk.items[OCC]
    assert f.stock_peak == 746.8
    desk.observe(OCC, 1.06, 1.14, ts + 1, 751.58)  # the stock's best: the half-way line is $735.13
    assert f.stock_fade == pytest.approx(735.125)
    assert desk.observe(OCC, 1.00, 1.10, ts + 10, 718.0) == []  # stock erased: STOPPED, seen once
    (e,) = desk.observe(OCC, 1.00, 1.10, ts + 40, 735.0)  # 30s later only half gone: the milder wins
    assert e.kind == "fading" and "given back half its move" in e.why
    desk.observe(OCC, 1.00, 1.10, ts + 50, 718.5)
    (e,) = desk.observe(OCC, 1.00, 1.10, ts + 80, 718.0)
    assert e.kind == "stopped" and "back to yesterday's close ($718.67)" in e.why
    # running again needs the stock's move back too
    assert desk.observe(OCC, 1.30, 1.40, ts + 90, 718.0) == []
    assert desk.observe(OCC, 1.31, 1.40, ts + 95, 718.0) == []
    (e,) = desk.observe(OCC, 1.32, 1.40, ts + 99, 740.0)
    assert e.kind == "rerun" and f.entry == 1.40 and f.state == RUNNING


def test_running_steps_gaps_and_caps():
    cfg = ocfg(follow_max_pushes=2, follow_gap_s=120)
    desk = FollowDesk(cfg)
    ts = at(12, 51)
    desk.track(meta_hit(meta_contract(1.05, 1.15, ts), 746.8, ts), ts)
    f = desk.items[OCC]
    assert f.next_run == pytest.approx(1.44)  # the $1.15 entry +25%
    assert desk.observe(OCC, 1.45, 1.50, ts + 10, 748.0) == []  # one quote is not a high
    (e,) = desk.observe(OCC, 1.46, 1.50, ts + 15, 748.0)
    assert (
        e.kind == "running"
        and e.severity == "MEDIUM"
        and f.high == 1.45
        and f.next_run == pytest.approx(1.81)
    )
    desk.observe(OCC, 1.90, 1.95, ts + 20, 749.0)
    assert desk.observe(OCC, 1.90, 1.95, ts + 25, 749.0) == []  # +25% again, but inside the 120s gap
    desk.observe(OCC, 2.40, 2.45, ts + 200, 749.0)
    (e,) = desk.observe(OCC, 2.40, 2.45, ts + 205, 749.0)
    assert e.kind == "running" and f.pushes == 2
    desk.observe(OCC, 3.10, 3.20, ts + 400, 749.0)
    assert desk.observe(OCC, 3.10, 3.20, ts + 405, 749.0) == []  # the per-contract cap
    desk.observe(OCC, 1.50, 1.60, ts + 410, 745.0)
    (e,) = desk.observe(OCC, 1.50, 1.60, ts + 440, 745.0)
    assert e.kind == "stopped"  # a stop is always sent
    assert f.stop == pytest.approx(2.02)  # 35% under the $3.10 best bid


def test_track_rules_restarts_and_lead_contracts():
    desk = FollowDesk(ocfg(follow_per_alert=1))
    ts = at(12, 51)
    hit = meta_hit(meta_contract(1.05, 1.15, ts), 746.8, ts)
    assert len(desk.track(hit, ts)) == 1 and desk.track(hit, ts + 60) == []  # already followed
    assert desk.underlyings() == {"META"} and desk.is_followed(OCC)
    flow = OptionsHit("META", "flow", "HIGH", hit.chain, None)
    assert desk.track(flow, ts) == []  # only spikes and blowups
    assert FollowDesk(ocfg(follow=False)).track(hit, ts) == []
    f = desk.items[OCC]
    f.state = STOPPED
    (g,) = desk.track(meta_hit(meta_contract(1.50, 1.60, ts + 600), 748.0, ts + 600), ts + 600)
    assert g.entry == 1.60 and g is not f  # a new alert after a stop is a new entry
    noquote = meta_contract(0, 0, ts)
    noquote.bid = noquote.ask = noquote.last = None
    assert desk.track(meta_hit(noquote, 746.0, ts), ts) == []  # nothing to enter at: not followed
    assert lead_contracts(flow) == []


def test_lead_contracts_of_a_spike_use_the_honest_base():
    from news247.market.options import SpikeRow

    c = meta_contract(1.05, 1.15, at(12, 51), prev=0.01)
    stale = SpikeRow(c, 10000.0, None, True, 0.20, True, 450.0)
    hit = OptionsHit("META", "spike", "HIGH", meta_chain(c, 746.8), None, spikes=[stale])
    assert lead_contracts(hit) == [(c, 0.20)]


def test_due_close_expire_and_status():
    desk = FollowDesk(ocfg(follow_hours=1))
    ts = at(12, 51)
    desk.track(meta_hit(meta_contract(1.05, 1.15, ts), 746.8, ts), ts)
    assert desk.due(ts + 10, 45) == set() and desk.due(ts + 50, 45) == {"META"}
    assert desk.expire(ts + 1800) == 0 and desk.expire(ts + 3700) == 1
    assert desk.items[OCC].ended == "time" and desk.active() == []
    desk2 = FollowDesk(ocfg())
    desk2.track(meta_hit(meta_contract(1.05, 1.15, ts), 746.8, ts), ts)
    assert desk2.close(at(16, 0)) == 1 and desk2.items[OCC].ended == "the session closed"
    assert desk2.observe(OCC, 2.0, 2.1, at(16, 1)) == []  # ended: no more follow-ups
    st = desk2.status()
    assert st["following"] == 0 and st["items"][0]["occ"] == OCC and st["rules"]["stop"] == 0.35
    assert desk2.expire(at(16, 2)) == 0 and desk2.close(at(16, 2)) == 0


def test_follows_survive_a_restart_but_not_a_new_day(tmp_path: Path):
    p = tmp_path / "options_follow.json"
    desk = FollowDesk(ocfg(), p)
    now = time.time()
    desk.track(meta_hit(meta_contract(1.05, 1.15, now), 746.8, now), now)
    desk.observe(OCC, 0.60, 0.70, now + 1, 746.0)
    desk.observe(OCC, 0.60, 0.70, now + 30, 746.0)
    again = FollowDesk(ocfg(), p)
    f = again.items[OCC]
    assert f.state == STOPPED and f.entry == 1.15 and again.pushed_today == 1 and again.events
    assert f._stop_frac == 0.35  # the rules are re-armed
    doc = json.loads(p.read_text())
    doc["day"] = "2000-01-01"
    p.write_text(json.dumps(doc))
    assert FollowDesk(ocfg(), p).items == {}
    p.write_text("not json")
    assert FollowDesk(ocfg(), p).items == {}


def test_daily_push_budget():
    desk = FollowDesk(ocfg(follow_daily_pushes=1))
    ts = at(12, 51)
    desk.track(meta_hit(meta_contract(1.05, 1.15, ts), 746.8, ts), ts)
    desk.observe(OCC, 1.50, 1.55, ts + 1, 748.0)
    assert len(desk.observe(OCC, 1.50, 1.55, ts + 2, 748.0)) == 1
    desk.observe(OCC, 1.20, 1.25, ts + 200, 747.0)
    assert desk.observe(OCC, 1.20, 1.25, ts + 230, 747.0) == []  # a fade over the day's budget: app only
    assert desk.items[OCC].state == FADING
    desk.observe(OCC, 0.90, 1.0, ts + 240, 747.0)
    assert [e.kind for e in desk.observe(OCC, 0.90, 1.0, ts + 270, 747.0)] == ["stopped"]  # stops always go


# --------------------------------------------------------------------------- the feed


class FakeSource:
    name = "cboe"
    label = "Fake"

    def __init__(self, chains: dict[str, Chain]) -> None:
        self.chains = chains
        self.reads: list[str] = []

    async def fetch(self, symbol: str, listing: Any, pace: Any) -> Chain:
        self.reads.append(symbol)
        return self.chains[symbol]


class FakeAlpaca(FakeSource):
    name = "alpaca"

    def __init__(self, chains: dict[str, Chain], bars: list | None = None, fail: bool = False) -> None:
        super().__init__(chains)
        self.bars = bars or []
        self.fail = fail
        self.bar_asks: list[list[str]] = []

    async def option_bars(self, occs: list[str], start: datetime, pace: Any) -> dict:
        self.bar_asks.append(occs)
        if self.fail:
            raise ValueError("no bars")
        return {occs[0]: list(self.bars)}

    async def stock_snapshots(self, symbols: list[str], pace: Any) -> dict:
        return {}


def meta_feed(src: Any, **kw: Any) -> OptionsFeed:
    u = uni(li("META", 1.88e12, 3.9), li("MIDB", 5e9), li("BIG", 300e9), li("LRG", 30e9))
    cfg = ocfg(**kw)
    return OptionsFeed(cfg, None, u, OptionsRadar(cfg), sources=[src], market_phase=lambda t: "open")


async def test_tracking_puts_the_contract_on_the_stream_and_its_quotes_send_follow_ups():
    now = time.time()
    c = meta_contract(1.05, 1.15, now)
    f = meta_feed(FakeSource({}))
    f.stream = OptionStream("wss://x", "k", "s", f._on_trigger, on_quote=f._on_stream_quote)
    got: list = []

    async def on_follow(evs):
        got.extend(evs)

    f._on_follow = on_follow
    hit = meta_hit(c, 746.8, now)
    (row,) = f.track(hit, now)
    w = f.stream.watch[OCC]
    assert w.follow and w.rank == 1e6 and f.priority["META"] > now + 6 * 3600  # hot until the close
    f.live.update("META", 748.0, META_PREV, now, "alpaca")
    f.stream.handle([{"T": "q", "S": OCC, "bp": 1.50, "ap": 1.55, "t": now + 1}], now + 1)
    f.stream.handle([{"T": "q", "S": OCC, "bp": 1.50, "ap": 1.55, "t": now + 2}], now + 2)
    for _ in range(50):
        if got:
            break
        await asyncio.sleep(0.01)
    (ev,) = got
    assert ev.kind == "running" and ev.f.stock == 748.0 and f.follow_events == 1
    st = f.status()
    assert st["follow"]["following"] == 1 and st["follow"]["events_sent"] == 1
    assert st["live"]["follow_reads"] == 0 and "jump_reads" in st["live"]
    other = OptionStream("wss://x", "k", "s", lambda *a: None, on_quote=lambda *a: 1 / 0)
    other.watch["X"] = type(w)("X", "META", 0.18, follow=True)
    other.handle([{"T": "q", "S": "X", "bp": 1.0, "ap": 1.1}])  # a bad hook never drops the socket


async def test_a_chain_read_follows_the_contract_and_the_play_card_comes_with_the_alert():
    now = at(12, 58)
    c = meta_contract(1.52, 1.69, now)
    ch = meta_chain(c, 746.475)
    for k, prev, bid in ((790, 0.15, 1.20), (795, 0.12, 0.90), (800, 0.11, 0.70)):  # the calls beside it
        o = meta_contract(bid, bid + 0.1, now, prev=prev)
        o.symbol, o.strike = f"META261014C00{k}000", float(k)
        ch.contracts.append(o)
    src = FakeAlpaca({"META": ch}, bars=CALL)
    f = meta_feed(src)
    f.live.update("META", 746.475, META_PREV, now, "alpaca")
    hits = await f.read("META", now)
    (h,) = [x for x in hits if x.kind == "blowup"]
    assert lead_contracts(h)[0][0].symbol == OCC  # the biggest multiple leads
    assert src.bar_asks == [[OCC]] and h.paths[OCC].high == 1.83 and f.bar_reads == 1
    assert h.play is not None and h.play.entry == 1.69 and h.play.path is not None
    assert h.to_dict()["play"]["entry"] == 1.69 and h.to_dict()["paths"][OCC]["legs"] == 3
    f.track(h, now)
    got: list = []

    async def on_follow(evs):
        got.extend(evs)

    f._on_follow = on_follow
    for i, bid in enumerate((1.20, 1.19)):  # two reads 30s apart, 25% under the alert's $1.605 mid
        src.chains["META"] = meta_chain(meta_contract(bid, bid + 0.1, now + 30 * (i + 1)), 745.0)
        await f.read("META", now + 30 * (i + 1))
    assert [e.kind for e in got] == ["fading"]
    bad = FakeAlpaca({"META": ch}, fail=True)
    g = meta_feed(bad)
    g.live.update("META", 746.475, META_PREV, now, "alpaca")
    (h2,) = [x for x in await g.read("META", now) if x.kind == "blowup"]
    assert g.bar_errors == 1 and h2.play is not None and h2.play.path is None  # no bars: still a card


async def test_follow_lane_rereads_quiet_contracts_and_ends_at_the_close():
    now = time.time()
    phase = {"v": "open"}
    f = meta_feed(FakeSource({}))
    f.market_phase = lambda t: phase["v"]
    f.track(meta_hit(meta_contract(1.05, 1.15, now), 746.8, now), now)
    f.follow_tick(now + 10)
    assert f._urgent == {}  # quoted 10s ago
    f.follow_tick(now + 50)
    assert f._urgent == {"META": "following up an alert"} and f.follow_reads == 1
    phase["v"] = "closed"
    f.follow_tick(now + 60)
    assert f.follow.active() == [] and f.follow.items[OCC].ended == "the session closed"
    meta_feed(FakeSource({})).follow_tick(now)  # nothing followed: nothing to do


def test_jumps_read_the_chain_at_once_by_company_size():
    f = meta_feed(FakeSource({}))
    t = time.time()
    f.live.update("META", 734.08, META_PREV, t - 300, "alpaca")  # the real 9:30 -> 9:35 closes
    f.live.update("META", 741.9351, META_PREV, t, "alpaca")
    f.live.update("BIG", 100.0, 100.0, t - 300, "alpaca")
    f.live.update("BIG", 100.9, 100.0, t, "alpaca")  # +0.9%, $300B: over 0.7%
    f.live.update("LRG", 100.0, 100.0, t - 300, "alpaca")
    f.live.update("LRG", 101.0, 100.0, t, "alpaca")  # +1.0%, $30B: under 1.2%
    f.live.update("MIDB", 10.0, 10.0, t - 300, "alpaca")
    f.live.update("MIDB", 9.7, 10.0, t, "alpaca")  # -3%, $5B: over 2%
    f.live.update("TINY", 1.0, 1.0, t - 300, "alpaca")
    f.live.update("TINY", 2.0, 1.0, t, "alpaca")  # not covered
    assert sorted(f.check_jumps(t)) == ["BIG", "META", "MIDB"]
    assert f._urgent["META"] == "jumped +1.1% in 5 min on a live price ($734.08 → $741.94)"
    assert f._urgent["MIDB"].startswith("jumped -3.0% in 5 min")
    assert f.urgent_why["META"][0].startswith("jumped +1.1%") and f.jump_reads == 3
    assert f.check_jumps(t + 5) == []  # checked 5s ago
    f.live.update("META", 748.0, META_PREV, t + 30, "alpaca")
    assert f.check_jumps(t + 30) == []  # the same direction inside the cooldown
    f.live.update("META", 735.0, META_PREV, t + 60, "alpaca")
    assert f.check_jumps(t + 60) == ["META"]  # a drop is its own direction


async def test_yahoo_quotes_seed_the_days_path():
    from aiohttp import web

    from .conftest import Recorder as _R  # noqa: F401

    class Http:
        async def get(self, url, params=None, headers=None, timeout_s=None):
            t0 = time.time() - 600
            body = {
                "META": {
                    "timestamp": [t0, t0 + 300, t0 + 590],
                    "close": [734.08, 751.58, 746.0],
                    "chartPreviousClose": META_PREV,
                }
            }

            class R:
                def json(self):
                    return body

            return R()

    assert web  # aiohttp is installed for the other tests
    f = meta_feed(FakeSource({}))
    f.http = Http()  # type: ignore[assignment]
    assert await f.yahoo_stocks(["META"]) == 1
    p = f.live.path("META")
    assert p is not None and p.high == 751.58 and f.live.get("META", 180).price == 746.0  # type: ignore[union-attr]


async def test_alpaca_snapshot_ranges_reach_the_path(server: Recorder, http: HttpClient):
    from aiohttp import web

    day0 = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)

    def snaps(request):
        return web.json_response({
            "META": {
                "latestTrade": {"t": utc(datetime.now(ET) - timedelta(seconds=3)), "p": 746.0},
                "dailyBar": {"t": utc(day0), "c": 746.0, "h": 751.58, "l": 727.64, "o": 728.495},
                "prevDailyBar": {"t": utc(day0 - timedelta(days=1)), "c": META_PREV},
            }
        })  # fmt: skip

    server.on("/v2/stocks/snapshots", snaps)
    cfg = alpaca_cfg(server)
    f = OptionsFeed(cfg, http, uni(li("META", 1.88e12)), OptionsRadar(cfg))
    assert await f.refresh_stocks(["META"]) == 1
    p = f.live.path("META")
    assert p is not None and (p.high, p.low, p.open) == (751.58, 727.64, 728.495)


# --------------------------------------------------------------------------- the engine's text


async def test_engine_alert_carries_the_play_and_follow_ups_publish(tmp_path: Path):
    from .test_options import EXP, NOW, SPIKE, chain_with, row

    eng, cap = engine(tmp_path)
    assert eng.options_feed is not None and eng.options_radar is not None
    hits = eng.options_radar.scan(
        chain_with(row(f"XYZ{EXP}C00060000", **SPIKE)), eng.universe.get("XYZ"), NOW
    )
    (hit,) = hits
    await eng.options_feed._play(hit, NOW)
    assert hit.play is not None
    await eng.on_options(hits)
    await asyncio.sleep(0.05)
    (a,) = cap.alerts
    assert "▶ If you buy now at the $2.10 ask (14.0x yesterday's $0.15):" in a.body
    assert "• Breakeven at expiry: XYZ $62.10 (+19.4% from here)" in a.body
    assert "• Each 1% XYZ moves your way: about $" in a.body
    assert "• Exits: fading under a $1.60 bid, stop under $1.30 (20% / 35% under the mid" in a.body
    assert "on the stock: half its move gone at $51.00, all of it at $50.00" in a.body
    assert "• Followed until the close" in a.body
    assert a.body.index("▶ If you buy now") < a.body.index("Cboe delayed quotes")
    assert eng.options_feed.follow.is_followed(f"XYZ{EXP}C00060000")  # tracked after it went out
    f = eng.options_feed.follow.items[f"XYZ{EXP}C00060000"]
    evs = eng.options_feed.follow.observe(f.occ, 1.20, 1.30, NOW + 10, 51.0, None, NOW + 10)
    evs += eng.options_feed.follow.observe(f.occ, 1.20, 1.30, NOW + 40, 51.0, None, NOW + 40)
    await eng.on_follow(evs)
    await asyncio.sleep(0.05)
    stop = cap.alerts[-1]
    assert stop.title.startswith(
        "🛑 XYZ $60 call (Oct 16) stop hit: bid $1.20, -43% since the 11:30 ET alert · XYZ $51.00"
    )
    assert (
        stop.severity == Severity.HIGH
        and "Why: the bid ($1.20) is 40% under the $2.00 mid when alerted." in stop.body
    )
    assert "XYZ $51.00: has given back 50% of its move today (yesterday $50.00, best $52.00" in stop.body
    assert "a bid back above $1.90 sends 'running again'" in stop.body
    assert stop.edge["follow"]["event"] == "stopped"
    evs = eng.options_feed.follow.observe(f.occ, 2.00, 2.10, NOW + 60, 52.5, None, NOW + 60)
    evs += eng.options_feed.follow.observe(f.occ, 2.00, 2.10, NOW + 65, 52.5, None, NOW + 65)
    await eng.on_follow(evs)
    await asyncio.sleep(0.05)
    rerun = cap.alerts[-1]
    assert rerun.title.startswith("🔁 XYZ $60 call (Oct 16) running again after its stop: bid $2.00")
    assert rerun.severity == Severity.MEDIUM and "New lines from here: ask $2.10" in rerun.body
    evs = eng.options_feed.follow.observe(f.occ, 2.70, 2.80, NOW + 300, 53.0, None, NOW + 300)
    evs += eng.options_feed.follow.observe(f.occ, 2.70, 2.80, NOW + 305, 53.0, None, NOW + 305)
    await eng.on_follow(evs)
    await asyncio.sleep(0.05)
    run = cap.alerts[-1]
    assert run.title.startswith(
        "📈 XYZ $60 call (Oct 16) still running: bid $2.70, +29% since the 11:30 ET alert"
    )
    assert "holding near its best level today" in run.body and "Lines now: fading under $2.16" in run.body
    evs = eng.options_feed.follow.observe(f.occ, 2.10, 2.20, NOW + 500, 53.0, None, NOW + 500)
    evs += eng.options_feed.follow.observe(f.occ, 2.10, 2.20, NOW + 530, 53.0, None, NOW + 530)
    await eng.on_follow(evs)
    await asyncio.sleep(0.05)
    fade = cap.alerts[-1]
    assert fade.title.startswith("⚠️ XYZ $60 call (Oct 16) fading: bid $2.10")
    assert "the option is losing value faster than the stock" in fade.body
    assert eng.options_board()["status"]["follow"]["following"] == 1
    live = eng.follow_for(cap.alerts[0].to_dict())  # the app's "after the alert" panel
    assert (
        live is not None
        and live["state"] == FADING
        and live["bid"] == 2.10
        and live["stop"] == pytest.approx(1.76)
    )
    assert eng.follow_for(fade.to_dict())["occ"] == f.occ  # type: ignore[index]  # a follow-up finds it too
    assert eng.follow_for({"edge": {}}) is None


def test_cli_prints_the_play(capsys):
    from news247.cli import _print_play

    now = at(12, 51)
    c = meta_contract(1.05, 1.15, now)
    play = play_card(c, meta_chain(c, 746.825), 0.18, now, ocfg(), path=option_path(CALL, 0.18))
    _print_play("META", None, play)
    _print_play("META", None, None)
    out = capsys.readouterr().out
    assert "if you buy now: $1.15 ask · breakeven at expiry META $786.15 (+5.3%)" in out
    assert "its day: high $1.83 at 11:20 ET · 3 leg(s) · trades through 12:40 ET" in out
    assert "37% under the day's $1.83 high: a pullback, not the run" in out
    assert "if META gives back half today's move: $" in out
    assert "exits: fading under a $0.88 bid, stop under $0.72 (both trail the best bid)" in out
