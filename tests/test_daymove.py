"""The day-move desk (market/daymove.py): a big stock's directional day, the options to buy, at
what price, and when the move is over. The stock tapes are real (tests/fixtures/daymove_tapes.json:
7 trend days and 3 days whose opening move failed, Sep 21 - Oct 12 2026); the study behind the
rules is docs/research/options-volume-strategy-2026-10-12.md.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from news247.config import ConfigError, build_config
from news247.market.daymove import DayCall, DayMoveDesk, VolumeTape, day_call, pick_contracts
from news247.market.live import LiveQuotes
from news247.market.options import Chain, Contract, OptionsFeed, OptionsHit, OptionsRadar, bs_price

from .test_options import ET, engine, li, ocfg, uni

TAPES = {
    (t["symbol"], t["day"]): t
    for t in json.loads((Path(__file__).parent / "fixtures" / "daymove_tapes.json").read_text())["tapes"]
}


@pytest.fixture(autouse=True)
def _no_alpaca_env(monkeypatch):
    for name in ("ALPACA_KEY", "ALPACA_SECRET", "ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)


def hm(ts: float) -> str:
    return datetime.fromtimestamp(ts, ET).strftime("%H:%M")


def bars(sym: str, day: str):
    """(price time, close) for each 5-minute bar: the close is the price at the bar's end."""
    t = TAPES[(sym, day)]
    return t["prev"], [(t["t0"] + 300 * i + 299, c) for i, c in enumerate(t["close"])]


def replay(sym: str, day: str, cfg: Any = None) -> list[str]:
    cfg = cfg or ocfg()
    prev, rows = bars(sym, day)
    lq, desk, log = LiveQuotes(), DayMoveDesk(cfg), []
    for ts, px in rows:
        lq.update(sym, px, prev, ts, "replay")
        for c in desk.scan(lq.quotes, lq.paths, {sym: 1e12}, ts):
            c.read = True
            log.append(f"{hm(ts)} call {c.side} {c.move_pct():+.2f}% ({c.how})")
        for e in desk.watch(ts):
            log.append(f"{hm(ts)} {e.kind}")
    return log


# --------------------------------------------------------------------------- the call, on the real tape


def test_the_calls_on_the_trend_days():
    assert replay("META", "2026-10-12") == ["09:49 call calls +3.71% (9:45 hold)", "11:24 zone"]
    assert replay("META", "2026-09-21") == ["09:49 call calls +5.69% (9:45 hold)", "11:04 zone"]
    assert replay("META", "2026-09-28") == ["09:49 call puts -3.99% (9:45 hold)", "12:24 zone"]
    assert replay("TSLA", "2026-10-02") == ["09:49 call calls +4.15% (9:45 hold)", "11:24 zone"]
    # fading at 9:45 (under its first five-minute close): no 9:45 call; at 10:00 it pressed its high
    assert replay("META", "2026-09-24") == ["10:04 call calls +2.36% (pressing its high)", "11:09 zone"]
    assert replay("AMD", "2026-10-12") == ["10:04 call puts -2.22% (pressing its low)", "11:44 zone"]


def test_a_sharp_early_dip_is_a_zone_not_a_fade():
    """MSFT Sep 25: back to 68% of its move at 10:14, +3.65% at the close; its $550 call went
    from $2.29 at the zone to $3.60."""
    assert replay("MSFT", "2026-09-25") == ["09:49 call calls +3.53% (9:45 hold)", "10:04 zone"]
    quick = ocfg(daymove_fade_s=0)
    assert replay("MSFT", "2026-09-25", quick)[-1] == "10:14 faded"  # without the 15 minutes it would quit


def test_the_fade_line_ends_a_failing_call():
    """MU today: called at 10:05 pressing its low, bounced to -2.9%; TSLA Sep 25: called puts on
    the reversal from its opening pop, then gave the drop back."""
    assert replay("MU", "2026-10-12") == [
        "10:04 call puts -4.42% (pressing its low)",
        "10:59 zone",
        "11:59 faded",
    ]
    assert replay("TSLA", "2026-09-25") == ["09:49 call puts -1.43% (9:45 hold)", "10:19 zone", "12:04 faded"]


def test_no_call_when_the_opening_move_fails():
    assert replay("NVDA", "2026-10-09") == []  # +1.5% gap, +0.6% after five minutes, red by 10:30
    assert replay("MSFT", "2026-10-01") == []  # +1.4% gap, +0.8% at 9:45, flat at the close


def test_day_call_rules():
    cfg = ocfg()
    prev, rows = bars("META", "2026-10-12")
    lq = LiveQuotes()
    for ts, px in rows[:3]:
        lq.update("META", px, prev, ts, "t")
    ts = rows[2][0]
    assert day_call("META", lq.get("META", 1e9, ts), lq.path("META", ts), ts, cfg) is None  # 9:44: too early
    lq.update("META", rows[3][1], prev, rows[3][0], "t")
    ts = rows[3][0]
    c = day_call("META", lq.get("META", 1e9, ts), lq.path("META", ts), ts, cfg)
    assert c is not None and c.cp == "C" and c.side == "calls" and c.how == "9:45 hold"
    assert day_call("META", None, lq.path("META", ts), ts, cfg) is None
    assert (
        day_call("META", lq.get("META", 1e9, ts), lq.path("META", ts), ts + 600, cfg) is None
    )  # a stale quote
    late = ocfg(daymove_until="09:50")
    assert day_call("META", lq.get("META", 1e9, ts), lq.path("META", ts), ts + 120, late) is None
    # a late start (no first five-minute bar seen) asks for 1.5x the move before 10:00
    lq2 = LiveQuotes()
    lq2.update("X", 101.2, 100.0, ts, "t")
    assert day_call("X", lq2.get("X", 1e9, ts), lq2.path("X", ts), ts, cfg) is None
    lq2.update("X", 101.6, 100.0, ts + 1, "t")
    c2 = day_call("X", lq2.get("X", 1e9, ts + 1), lq2.path("X", ts + 1), ts + 1, cfg)
    assert c2 is not None and c2.how == "9:45 hold"


def test_day_call_lines_and_kept():
    c = DayCall("META", "C", 0.0, 745.2, 718.67, 745.2, "9:45 hold")
    assert c.kept(745.2) == pytest.approx(1.0) and c.line(0.7) == pytest.approx(737.241, abs=0.001)
    c.observe(751.58)
    assert c.best == 751.58 and c.kept(745.2) == pytest.approx(0.806, abs=0.001)
    p = DayCall("MU", "P", 0.0, 980.4, 1028.135, 980.4, "9:45 hold")
    p.observe(978.5)
    p.observe(990.0)
    assert p.best == 978.5 and p.kept(990.0) == pytest.approx(0.768, abs=0.001) and p.sign == -1
    assert DayCall("Z", "C", 0, 1, 1, 1, "x").kept(1) == 0.0
    d = c.to_dict()
    assert d["side"] == "calls" and d["move_pct"] == pytest.approx(3.69, abs=0.01) and d["picks"] == []


def test_desk_one_call_a_company_a_day_and_the_daily_cap():
    cfg = ocfg(daymove_daily=1)
    prev, rows = bars("META", "2026-10-12")
    lq, desk = LiveQuotes(), DayMoveDesk(cfg)
    for ts, px in rows[:4]:
        lq.update("META", px, prev, ts, "t")
        lq.update("TSLA", 380.0 * px / prev, 380.0, ts, "t")  # the same shape
    ts = rows[3][0]
    (c,) = desk.scan(lq.quotes, lq.paths, {"META": 1.9e12, "TSLA": 1.5e12}, ts)
    assert c.symbol == "META" and desk.made_today == 1  # the biggest first, then the cap
    assert desk.scan(lq.quotes, lq.paths, {"META": 1.9e12, "TSLA": 1.5e12}, ts + 60) == []
    assert desk.watch(ts) == []  # its chain has not been read yet
    nxt = ts + 86400
    desk.scan({}, {}, {}, nxt)
    assert desk.calls == {} and desk.made_today == 0  # a new day
    assert DayMoveDesk(ocfg(daymove=False)).scan(lq.quotes, lq.paths, {"META": 1.9e12}, ts) == []


def test_session_path_ignores_the_premarket():
    lq = LiveQuotes()
    d = datetime(2026, 10, 12, 8, 0, tzinfo=ET).timestamp()
    lq.update("META", 760.0, 718.67, d, "t")  # 8:00: a premarket spike
    lq.update("META", 734.0, 718.67, d + 5400 + 100, "t")  # 9:31
    lq.update("META", 741.9, 718.67, d + 5400 + 299, "t")  # 9:34:59: the first bar's close
    lq.update("META", 745.2, 718.67, d + 5400 + 900, "t")  # 9:45
    p = lq.path("META", d + 5400 + 900)
    assert p is not None and p.high == 760.0 and p.s_high == 745.2 and p.s_low == 734.0
    assert p.first5 == 741.9 and p.s_first_ts == d + 5400 + 100


# --------------------------------------------------------------------------- the contracts


NOW = datetime(2026, 10, 12, 9, 50, tzinfo=ET).timestamp()
TODAY = date(2026, 10, 12)


def priced(
    s: float, k: float, days: int, cp: str = "C", iv: float = 0.45, spread: float = 0.04, sym: str = "META"
) -> Contract:
    exp = TODAY + timedelta(days=days)
    t = max(
        (datetime(exp.year, exp.month, exp.day, 16, tzinfo=ET).timestamp() - NOW) / (365 * 86400),
        3600 / (365 * 86400),
    )
    mid = bs_price(s, k, t, iv, cp)
    half = max(mid * spread / 2, 0.005)
    return Contract(
        f"{sym}{exp:%y%m%d}{cp}{int(k * 1000):08d}",
        sym,
        exp,
        cp,
        k,
        bid=round(mid - half, 2),
        ask=round(mid + half, 2),
        prev_close=round(mid, 2),
        volume=100,
        open_interest=1000,
        quote_time=datetime.fromtimestamp(NOW - 5, ET),
    )


def chain_at(s: float = 745.2, prev: float = 718.67, cp: str = "C", days=(0, 2, 4, 11, 18, 32, 60)) -> Chain:
    ch = Chain("META", price=s, prev_close=prev, change_pct=(s / prev - 1) * 100, source="t")
    for d in days:
        for k in range(690, 830, 5):
            ch.contracts.append(priced(s, float(k), d, cp))
    ch.quote_time = datetime.fromtimestamp(NOW - 5, ET)
    return ch


def meta_call() -> DayCall:
    return DayCall("META", "C", NOW, 745.2, 718.67, 745.2, "9:45 hold", last=745.2)


def test_picks_by_delta_and_tier():
    cfg = ocfg()
    picks = pick_contracts(chain_at(), meta_call(), NOW, cfg)
    day, swing = picks
    assert day.tier == "day" and 1 <= day.dte <= 7 and abs(day.delta - 0.25) < 0.04
    assert swing.tier == "swing" and 14 <= swing.dte <= 35 and abs(swing.delta - 0.30) < 0.04
    assert day.cp == "C" and day.strike > 745.2  # out of the money
    assert day.breakeven == pytest.approx(day.strike + day.ask)
    assert 0.40 < day.iv < 0.50  # the vol the chain was priced at
    assert day.mid > day.zone_hi > day.zone_lo > 0  # a pullback is cheaper; deeper is cheaper still
    assert day.leverage > swing.leverage > 0  # nearer expiry: more % per 1% of stock
    assert day.per_1pct > 0 and day.to_dict()["occ"] == day.occ
    with_lotto = pick_contracts(chain_at(), meta_call(), NOW, ocfg(daymove_lotto=True))
    assert [p.tier for p in with_lotto] == ["day", "swing", "lotto"] and with_lotto[2].dte <= 1


def test_picks_skip_wide_markets_and_the_wrong_side():
    cfg = ocfg()
    ch = chain_at(days=(4,))
    best = pick_contracts(ch, meta_call(), NOW, cfg)[0]
    for c in ch.contracts:
        if c.symbol == best.occ:
            c.bid, c.ask = round(best.mid * 0.6, 2), round(best.mid * 1.4, 2)  # an 80% wide market
    again = pick_contracts(ch, meta_call(), NOW, cfg)[0]
    assert again.occ != best.occ and again.spread_pct <= cfg.daymove_max_spread
    puts = chain_at(cp="P", days=(4,))
    assert pick_contracts(puts, meta_call(), NOW, cfg) == []  # an up day buys calls
    put_call = DayCall("META", "P", NOW, 700.0, 718.67, 700.0, "9:45 hold", last=700.0)
    (pp,) = pick_contracts(chain_at(s=700.0, cp="P", days=(4,)), put_call, NOW, cfg)
    assert pp.cp == "P" and pp.strike < 700.0 and pp.breakeven == pytest.approx(pp.strike - pp.ask)
    assert pp.mid > pp.zone_hi > pp.zone_lo
    blind = chain_at(days=(4,))
    blind.price = None
    assert pick_contracts(blind, DayCall("META", "C", NOW, 0, 718.67, 0, "x"), NOW, cfg) == []


def test_volume_tape_states():
    v = VolumeTape()
    assert v.state() == "too early to tell"
    v.add(0, 0)
    v.add(300, 500)  # 100 a minute
    assert v.state() == "building" and v.peak == 100
    v.add(600, 900)  # 80 a minute
    assert v.state() == "still heavy"
    v.add(900, 1000)  # 20
    assert v.state() == "past its peak"
    v.add(1200, 1050)  # 10
    assert v.state() == "dried up"
    v.add(1100, 2000)  # out of order: ignored
    v.add(1500, 900)  # a lower cumulative count: ignored
    assert v.samples[-1] == (1200, 1050)


# --------------------------------------------------------------------------- the feed and the alert


class FakeSource:
    name = "cboe"
    label = "Fake"

    def __init__(self, chain: Chain) -> None:
        self.chain = chain
        self.reads = 0

    async def fetch(self, symbol: str, listing: Any, pace: Any) -> Chain:
        self.reads += 1
        return self.chain


def meta_feed(chain: Chain, **kw: Any) -> OptionsFeed:
    cfg = ocfg(**kw)
    return OptionsFeed(
        cfg, None, uni(li("META", 1.9e12, 3.7), li("SMALL", 5e9)), OptionsRadar(cfg), sources=[FakeSource(chain)],
        market_phase=lambda t: "open",
    )  # fmt: skip


async def test_feed_calls_reads_and_alerts_then_the_zone():
    prev, rows = bars("META", "2026-10-12")
    f = meta_feed(chain_at())
    for ts, px in rows[:4]:
        f.live.update("META", px, prev, ts, "alpaca")
    t_call = rows[3][0]
    f.daymove_tick(t_call)
    assert f._urgent["META"] == "day move: META +3.7% (9:45 hold)" and f._daymove_pending == {"META": "call"}
    hits = await f.read("META", t_call)
    (dm,) = [h for h in hits if h.kind == "daymove"]
    assert dm.stage == "call" and dm.severity == "HIGH" and len(dm.daycall.picks) == 2
    assert any("read at once: day move" in r for r in dm.reasons)
    assert dm.to_dict()["daycall"]["picks"][0]["tier"] == "day" and dm.to_dict()["stage"] == "call"
    assert [h for h in await f.read("META", t_call + 60) if h.kind == "daymove"] == []  # sent once
    for ts, px in rows[4:]:
        f.live.update("META", px, prev, ts, "alpaca")
        f.daymove_tick(ts)
        if f._daymove_pending:
            break
    assert hm(ts) == "11:24" and f._daymove_pending == {"META": "zone"}
    f.sources[0].chain.quote_time = datetime.fromtimestamp(ts, ET)  # a fresh chain at 11:24
    (z,) = [h for h in await f.read("META", ts) if h.kind == "daymove"]
    assert z.stage == "zone" and z.severity == "MEDIUM"
    st = f.status()["daymove"]
    assert st["calls_today"] == 1 and st["calls"][0]["state"] == "zone" and st["events"][0]["kind"] == "zone"


async def test_feed_sends_the_fade_without_a_chain_read():
    prev, rows = bars("MU", "2026-10-12")
    ch = chain_at(s=984.25, prev=prev, cp="P")
    for c in ch.contracts:
        c.underlying = "MU"
    ch.symbol = "MU"
    cfg = ocfg()
    f = OptionsFeed(cfg, None, uni(li("MU", 1.1e12)), OptionsRadar(cfg), sources=[FakeSource(ch)],
                    market_phase=lambda t: "open")  # fmt: skip
    got: list[OptionsHit] = []

    async def on_hits(hs):
        got.extend(hs)

    f._on_hits = on_hits
    for ts, px in rows:
        f.live.update("MU", px, prev, ts, "alpaca")
        f.daymove_tick(ts)
        if f._daymove_pending:
            await f.read("MU", ts)
        await asyncio.sleep(0)
    await asyncio.sleep(0.01)
    (faded,) = got
    assert faded.kind == "daymove" and faded.stage == "faded" and faded.severity == "MEDIUM"
    assert "given back" in faded.reasons[0] and "0 of 6 option spikes closed higher" in faded.reasons[0]


def test_feed_daymove_off_and_small_companies():
    prev, rows = bars("META", "2026-10-12")
    f = meta_feed(chain_at(), daymove=False)
    for ts, px in rows[:4]:
        f.live.update("META", px, prev, ts, "alpaca")
    f.daymove_tick(rows[3][0])
    assert f._urgent == {}
    g = meta_feed(chain_at())
    for ts, px in rows[:4]:
        g.live.update("SMALL", px / 10, prev / 10, ts, "alpaca")  # the same move on a $5B name
    g.daymove_tick(rows[3][0])
    assert g._urgent == {}  # under daymove_min_cap


async def test_engine_alert_text_and_follow(tmp_path: Path):
    from news247.market.universe import Listing

    eng, cap = engine(tmp_path)
    assert eng.options_feed is not None
    call = meta_call()
    ch = chain_at()
    call.picks = pick_contracts(ch, call, NOW, eng.cfg.options)
    hit = OptionsHit("META", "daymove", "HIGH", ch, Listing("META", "Meta Platforms", market_cap=1.9e12),
                     daycall=call, stage="call", detected=NOW)  # fmt: skip
    a = eng._options_alert(hit)
    assert a.title.startswith(
        "📊 META day move: calls · META up 3.7% and holding (9:45 hold) · Meta Platforms"
    )
    day = call.picks[0]
    assert f"• Day trade: {day.label} ({day.dte}d): ask ${day.ask:.2f} / bid ${day.bid:.2f}" in a.body
    assert f"Pullback buy zone ${day.zone_hi:.2f}–${day.zone_lo:.2f} (with META $741.22–$737.24)" in a.body
    assert "• Trend (multi-day):" in a.body and "The call is off if META stays under $737.24" in a.body
    assert "median +14% by the close (7 of 11 up)" in a.body and "The system never trades." in a.body
    assert a.edge["options"]["daycall"]["how"] == "9:45 hold"
    await eng.on_options([hit])
    assert eng.options_feed.follow.is_followed(day.occ)  # the day pick is followed after the alert
    zone = OptionsHit("META", "daymove", "MEDIUM", ch, None, daycall=call, stage="zone", detected=NOW)
    z = eng._options_alert(zone)
    assert z.title.startswith("🔁 META pullback zone: +3.7%, 100% of its move held · the calls' buy zone")
    assert "volume too early to tell" in z.body
    call.last = 733.0
    faded = OptionsHit("META", "daymove", "MEDIUM", ch, None, daycall=call, stage="faded", detected=NOW,
                       reasons=["META $733.00 has given back 45% of its move"])  # fmt: skip
    fa = eng._options_alert(faded)
    assert fa.title.startswith("⛔ META day move faded: +2.0%, under 70% of its move for 15 min")
    assert "is off: no new entries" in fa.body and "given back 45%" not in fa.body
    assert not eng.options_feed.follow.track(faded)  # a fade is not something to follow


def test_daymove_config_validation():
    for bad in (
        {"daymove_until": "09:30"},
        {"daymove_until": "nope"},
        {"daymove_fade": 0.95},
        {"daymove_min_pct": 0},
        {"daymove_day_min_dte": 9},
        {"daymove_swing_delta": 0.6},
        {"daymove_max_spread": 0},
    ):
        with pytest.raises(ConfigError):
            build_config({"options": bad})
    assert build_config({"options": {"daymove_until": "14:30"}}).options.daymove_until_min == 870
