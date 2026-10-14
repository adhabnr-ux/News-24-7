"""Ignition: stocks starting to move, read on the live tape, minutes before the radar's day-change
ladder would say so. Real 1-minute bars (tests/fixtures/ignition_days.json) replay through the
scanner the way the tape feeds it; the rest are the rules one by one."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from news247.config import build_config
from news247.market.ignition import PROFILE, IgnitionScanner, Tick, session_minute, volume_share

from .conftest import FIXTURES

ET_DAYS = json.loads((FIXTURES / "ignition_days.json").read_text())["days"]
CFG = build_config({}).smallcap


def replay(day: dict, scanner: IgnitionScanner | None = None):
    """Feed one session minute by minute (each bar known at its end): [(minute, hit)]."""
    sc = scanner or IgnitionScanner(CFG)
    cum = pv = 0.0
    hi = lo = None
    out = []
    for i, (c, h, low, v) in enumerate(day["bars"]):
        cum += v
        pv += v * (h + low + c) / 3
        hi = h if hi is None else max(hi, h)
        lo = low if lo is None else min(lo, low)
        ts = day["t0"] + 60 * i + 59
        tick = Tick(day["symbol"], ts, c, day["prev_close"], cum, day["base_volume"], hi, lo, pv / cum if cum else None,
                    day["market_cap"], day["symbol"])  # fmt: skip
        for hit in sc.observe([tick], now=ts):
            out.append((i, hit))
    return out


def radar_crossing(day: dict, direction: int) -> int | None:
    """The minute the radar's day-change line was crossed (its ladder's first rung)."""
    sc = IgnitionScanner(CFG)
    line = sc.day_threshold(day["market_cap"])
    for i, (c, *_rest) in enumerate(day["bars"]):
        if direction * (c / day["prev_close"] - 1) * 100 >= line:
            return i
    return None


def hhmm(i: int) -> str:
    return f"{9 + (i + 30) // 60}:{(i + 30) % 60:02d}"


def day(sym: str) -> dict:
    return next(d for d in ET_DAYS if d["symbol"] == sym)


@pytest.mark.parametrize(
    ("sym", "first", "direction", "radar"),
    [
        ("FNMA", "9:37", "down", "9:44"),  # Burry's warning: -19.9% by 11:34
        ("FMCC", "9:35", "down", "9:42"),
        ("CBRS", "9:47", "down", "9:51"),  # the chip selloff
        ("NVAX", "13:46", "up", "14:47"),  # the plague scare: +28% by 15:38, the radar an hour later
        ("META", "9:37", "up", "10:47"),
    ],
)
def test_real_moves_ignite_before_the_radar(sym, first, direction, radar):
    d = day(sym)
    hits = [(i, h) for i, h in replay(d) if h.direction == direction]
    assert hits, f"{sym}: no ignition"
    i, hit = hits[0]
    assert hhmm(i) == first, (sym, hhmm(i), hit.kind)
    r = radar_crossing(d, 1 if direction == "up" else -1)
    assert hhmm(r) == radar and r > i


def test_a_quiet_day_stays_quiet():
    assert replay(day("V")) == []  # Visa, Oct 8: no push big enough on enough volume


def test_the_hit_carries_entry_stop_and_volume():
    i, hit = replay(day("FNMA"))[0]
    assert hit.kind == "ignition" and hit.direction == "down"
    assert hit.price < hit.launch and hit.move_pct <= -CFG.ignition_pct_mid
    assert hit.burst >= CFG.ignition_burst and hit.rvol > 1
    assert hit.stop > hit.price and 0 < hit.risk_pct < 10  # a down move fails above the launch / VWAP
    d = hit.to_dict()
    assert d["band"] == "mid cap" and d["cap"].startswith("$4") and d["window_s"] >= 240


def test_fires_once_per_direction_and_day():
    hits = replay(day("FMCC"))
    kinds = [(h.kind, h.direction) for _, h in hits]
    assert len(kinds) == len(set(kinds))


# --------------------------------------------------------------------------- the rules


def T(price, volume, ts, *, prev=100.0, base=1_000_000, hi=None, lo=None, cap=5e9, vwap=None, sym="ACME"):
    return Tick(sym, ts, price, prev, volume, base, hi if hi is not None else price, lo if lo is not None else price,
                vwap, cap)  # fmt: skip


def at(h: int, m: int, s: int = 0) -> float:
    from zoneinfo import ZoneInfo

    return datetime(2026, 10, 14, h, m, s, tzinfo=ZoneInfo("America/New_York")).timestamp()


def test_profile_and_session_clock():
    assert PROFILE[0] == 0.0 and PROFILE[-1] == 1.0
    assert all(b >= a for a, b in zip(PROFILE, PROFILE[1:]))
    assert 0.08 < volume_share(0, 30) < 0.11  # the first half hour: ~9% of the day
    assert 0.15 < volume_share(0, 60) < 0.2  # the first hour: ~17%
    assert 0.1 < volume_share(385, 390) < 0.15  # the last 5 minutes and the close: ~12%
    assert session_minute(at(9, 45)) == pytest.approx(15)
    assert session_minute(at(9, 29)) is None and session_minute(at(16, 1)) is None


def push(sc, *, move=2.0, burst=6.0, start=(10, 30), prev=100.0, before_pct=0.0, cap=5e9, sym="ACME"):
    """Two ticks five minutes apart: the stock moves ``move``% on ``burst``x normal volume."""
    p0 = prev * (1 + before_pct / 100)
    t0 = at(*start)
    m0 = session_minute(t0) or 0.0
    v0 = 1_000_000 * volume_share(0, m0)
    sc.observe([T(p0, v0, t0, prev=prev, hi=p0, lo=p0, cap=cap, sym=sym)], now=t0)
    t1 = t0 + 300
    p1 = p0 * (1 + move / 100)
    v1 = v0 + burst * 1_000_000 * volume_share(m0, m0 + 5)
    return sc.observe([T(p1, v1, t1, prev=prev, hi=max(p0, p1), lo=min(p0, p1), cap=cap, sym=sym)], now=t1)


def test_needs_the_move_the_volume_and_a_new_extreme():
    assert len(push(IgnitionScanner(CFG))) == 1  # mid cap: 2% on 6x
    assert push(IgnitionScanner(CFG), move=1.2) == []  # under 1.5%
    assert push(IgnitionScanner(CFG), burst=2.5) == []  # not on volume
    assert len(push(IgnitionScanner(CFG), move=-2.0)) == 1
    # big companies need less, small ones more
    assert len(push(IgnitionScanner(CFG), move=0.7, cap=500e9)) == 1
    assert push(IgnitionScanner(CFG), move=2.5, cap=800e6) == []


def test_an_already_big_mover_is_the_ladders_not_an_ignition():
    # +11% on the day before the push: past the mid-cap radar line (10%)
    assert push(IgnitionScanner(CFG), before_pct=11.0) == []
    assert len(push(IgnitionScanner(CFG), before_pct=4.0)) == 1


def test_not_before_the_open_settles():
    assert push(IgnitionScanner(CFG), start=(9, 25)) == []  # the window starts pre-market


def test_priming_lowers_the_bar_and_names_the_reason():
    sc = IgnitionScanner(CFG)
    sc.prime("ACME", "up", "options: calls flow, $2.1M premium", now=at(10, 0))
    hit = push(sc, move=1.0, burst=3.5)  # 1.0% >= 0.6 x 1.5%, 3.5x >= 3x
    assert len(hit) == 1 and hit[0].primed == ["options: calls flow, $2.1M premium"]
    # a prime on the other side does not count, and primes expire
    sc2 = IgnitionScanner(CFG)
    sc2.prime("ACME", "down", "insider", now=at(10, 0))
    assert push(sc2, move=1.0, burst=3.5) == []
    sc3 = IgnitionScanner(CFG)
    sc3.prime("ACME", "up", "old", hold_s=60, now=at(9, 0))
    assert push(sc3, move=1.0, burst=3.5) == []


def test_thin_feeds_are_not_read():
    sc = IgnitionScanner(CFG)
    t0 = at(10, 30)
    sc.observe([T(100, 10, t0, base=2_000)], now=t0)
    assert sc.observe([T(103, 900, t0 + 300, base=2_000, hi=103)], now=t0 + 300) == []


def test_buildup_on_a_steady_climb():
    sc = IgnitionScanner(CFG)
    prev = 100.0
    out = []
    # a mid cap grinding up 0.25% every 5 minutes from 10:00 on 3x normal volume, never a burst
    for k in range(40):
        ts = at(10, 0) + 300 * k
        m = session_minute(ts)
        price = prev * (1 + 0.0025 * k)
        vol = 3 * 1_000_000 * volume_share(0, m)
        out += sc.observe([T(price, vol, ts, prev=prev, hi=price, lo=prev, vwap=prev * 1.02)], now=ts)
    assert [h.kind for h in out] == ["buildup"]
    hit = out[0]
    assert 5.0 <= hit.day_pct < 10.0 and hit.rvol >= 2.0
    assert hit.stop == pytest.approx(102.0)  # the VWAP is nearer than yesterday's close


def test_stop_is_the_nearer_of_launch_and_vwap_on_the_right_side():
    s = IgnitionScanner._stop
    assert s("up", 100.0, 101.0, 103.0) == 101.0
    assert s("up", 100.0, 104.0, 103.0) == 100.0  # VWAP above the price: the launch
    assert s("down", 100.0, 99.0, 97.0) == 99.0
    assert s("down", 100.0, None, 97.0) == 100.0


def test_repeated_or_old_quotes_are_ignored():
    sc = IgnitionScanner(CFG)
    t0 = at(10, 30)
    sc.observe([T(100, 100_000, t0)], now=t0)
    assert sc.observe([T(105, 900_000, t0 - 10)], now=t0) == []
    assert len(sc.state["ACME"].points) == 1
