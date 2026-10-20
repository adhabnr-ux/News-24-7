"""Run 35: the flow record (options alerts get a direction, a shape and a reference price, so the
options lane is graded) and event day (a push the morning of a binary event on your stock, and
each event's implied move graded against what it did)."""

from __future__ import annotations

import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from news247.dossier import _direction
from news247.flowrec import (
    flow_base,
    flow_board,
    flow_direction,
    flow_line,
    flow_rates,
    flow_shape,
    keys,
    stamp,
)
from news247.models import Alert, Severity
from news247.stakes import event_record, grade_events, log_events, send_event_day

from .test_desk import DEV
from .test_personal import semis_engine
from .test_run3 import phones

ET = ZoneInfo("America/New_York")


def row(kind="call", strike=108.0, dte=5, premium=600_000, side="near the ask (likely bought)"):
    return {"type": kind, "strike": strike, "dte": dte, "premium": premium, "side": side}


def opt(flow=None, kind="flow", price=100.0, **extra):
    d = {"kind": kind, "stock_price": price, "flow": flow or [], "spikes": [], "blowups": [],
         "call_premium": sum(r["premium"] for r in flow or [] if r["type"] == "call"),
         "put_premium": sum(r["premium"] for r in flow or [] if r["type"] == "put"), **extra}  # fmt: skip
    d["flow_premium"] = d["call_premium"] + d["put_premium"]
    return d


# --------------------------------------------------------------------------- reading the flow


def test_the_direction_of_the_flow():
    assert flow_direction(opt([row()])) == "up"  # calls bought
    assert flow_direction(opt([row("put", 90)])) == "down"  # puts bought
    assert flow_direction(opt([row(side="near the bid (likely sold)")])) == "down"  # calls sold
    assert flow_direction(opt([row("put", 90, side="near the bid (likely sold)")])) == "up"  # puts sold
    assert flow_direction(opt([row(), row("put", 90)])) == ""  # balanced: no call
    assert (
        flow_direction(opt([row(premium=900_000, side="mid-spread"), row("put", 90, premium=100_000)]))
        == "up"
    )
    assert flow_direction(opt(kind="blowup", side="puts")) == "down"
    assert (
        flow_direction(opt(kind="spike", spikes=[{"type": "call", "dte": 2, "strike": 120, "premium": 1000}]))
        == "up"
    )
    assert flow_direction(opt(kind="surge", call_premium=3e6, put_premium=1e6)) == "up"


def test_the_shape_of_the_flow():
    s = flow_shape(opt([row(premium=1_200_000)]))
    assert s == {"lane": "flow", "side": "calls", "dte": "0-7 days", "money": "2-10% out", "size": "$1M+",
                 "label": "OTM call flow, 0-7 days, $1M+"}  # fmt: skip
    assert (
        flow_shape(opt([row("put", 99, dte=45, premium=100_000)]))["label"]
        == "put flow, 31+ days, under $250K"
    )
    assert flow_shape(opt([row(strike=125)]))["money"] == "10%+ out"
    assert keys(s)[0] == "flow|calls|0-7 days|2-10% out|$1M+" and keys(s)[-1] == "*|calls"
    assert flow_shape(opt()) is None


def test_an_options_alert_is_stamped_and_graded(tmp_path):
    a = Alert(
        kind="options", severity=Severity.HIGH, title="🐋 Unusual options: ACME", body="", tickers=["ACME"]
    )
    a.edge["options"] = opt([row()])
    stamp(a)
    assert a.edge["flow"]["dir"] == "up" and a.edge["refs"] == {"ACME": 100.0}
    assert _direction(a.to_dict()) == "up"  # every graded feature now reads options alerts too


def graded_flow(eng, n, close, shape_row, title="ACME flow"):
    for i in range(n):
        a = Alert(kind="options", severity=Severity.HIGH, title=f"{title} {i}", body="", tickers=["ACME"])
        a.edge["options"] = opt([shape_row])
        stamp(a)
        a.edge["outcome"] = {
            "ACME": {
                "h1": None,
                "close": close[i % len(close)],
                "d1": None,
                "sign": 1 if a.edge["flow"]["dir"] == "up" else -1,
            }
        }
        a.created = time.time() - 86400
        eng.storage.add_alert(a)


def test_the_record_of_each_shape(tmp_path):
    eng = semis_engine(tmp_path)
    graded_flow(
        eng, 10, [2.0, 1.0, 3.0, -1.0], row(premium=1_200_000)
    )  # OTM call buying, near-dated, big: worked
    graded_flow(
        eng, 10, [1.0, 2.0, 1.5, 0.5, 2.5], row("put", 99, dte=45, premium=100_000)
    )  # puts bought; the stock rose
    rates = flow_rates(eng)
    big = rates["flow|calls|0-7 days|2-10% out|$1M+"]
    assert (
        big["close"] == {"n": 10, "hit": 0.8, "median": 1.5}
        and big["label"] == "flow calls, 0-7 days, 2-10% out, $1M+"
    )
    puts = rates["flow|puts"]["close"]
    assert puts["hit"] == 0.0  # the puts called it down; the stock went up every time
    fb = flow_base(rates, flow_shape(opt([row(premium=2_000_000)])))
    assert fb["key"] == "flow|calls|0-7 days|2-10% out|$1M+"
    assert (
        flow_line(fb)
        == "Flow like this (flow calls, 0-7 days, 2-10% out, $1M+): 80% went its way by the close (median +1.5%, 10)"
    )
    # 8-30 days: none graded with that shape, so the coarser record for call flow stands in
    assert flow_base(rates, flow_shape(opt([row(dte=20)])))["key"] == "flow|calls"
    board = flow_board(rates)
    assert board["best"][0]["key"].startswith("flow|calls") and board["worst"][0]["key"].startswith(
        "flow|puts"
    )


async def test_a_new_options_alert_carries_its_record(tmp_path):
    eng = semis_engine(tmp_path)
    graded_flow(eng, 10, [2.0, 1.0, 3.0, -1.0], row(premium=1_200_000))
    eng.refresh_base_rates()
    a = Alert(
        kind="options", severity=Severity.HIGH, title="🐋 Unusual options: ACME", body="", tickers=["ACME"]
    )
    a.edge["options"] = opt([row(premium=1_500_000)])
    stamp(a)
    from news247.flowrec import flow_base as fbase

    a.edge["flow_base"] = fbase(eng.flow_rates, a.edge["flow"])
    await eng.publish(a, push=False)
    assert "80% went its way" in eng.webpush.payload(a)["body"]


# --------------------------------------------------------------------------- event day

DAY = datetime(2026, 10, 14, 8, 30, tzinfo=ET).timestamp()  # a Wednesday morning


def pdufa(eng):
    eng.storage.add_catalyst({"id": "GAMA:2026-10-14:pdufa", "date": "2026-10-14", "time": "", "title": "GAMA · FDA decision (PDUFA)",
                              "kind": "binary", "event": "pdufa", "symbol": "GAMA", "headline": "x"})  # fmt: skip
    eng.live_quotes.update("GAMA", 20.0, 20.0, DAY - 5, "tape")
    eng.implied_cache["GAMA"] = (DAY, {"rows": [{"expiry": "2026-10-16", "move_pct": 25.0}]})


async def test_the_morning_of_the_decision(tmp_path, server):
    for p in ("/push/a", "/push/b"):
        server.on(p, (201, ""))
    eng = semis_engine(tmp_path)
    from .test_run32 import stakes_history

    stakes_history(eng)
    pdufa(eng)
    await eng.http.start()
    try:
        a, _ = phones(eng, server)
        eng.desk.apply(DEV, "hold_set", {"symbol": "GAMA", "shares": 100})
        assert await send_event_day(eng, DAY - 3600) == 0  # 07:30: too early
        assert await send_event_day(eng, DAY) == 1
        msg = a.read(server.requests[0]["body"])
        assert msg["title"] == "📅 GAMA's FDA decision is today"
        assert (
            msg["body"]
            == "Here, if an approval: +10.0% (5) · if a rejection or delay: -27.5% (4)\noptions price ±25.0% · you hold $2,000 (±$500)"
        )
        assert await send_event_day(eng, DAY + 60) == 0  # once
        log = json.loads(eng.storage.get_setting("event_log"))
        assert [(e["symbol"], e["ref"], e["implied"]) for e in log] == [("GAMA", 20.0, 25.0)]
        eng.desk.apply(DEV, "prefs", {"event_day": False})
        eng.storage.set_setting("eventday:2026-10-14", "[]")
        assert await send_event_day(eng, DAY + 120) == 0
    finally:
        await eng.http.close()


def test_events_are_graded_against_what_options_priced(tmp_path):
    eng = semis_engine(tmp_path)
    pdufa(eng)
    assert log_events(eng, DAY) == 1 and log_events(eng, DAY + 60) == 0
    second = datetime(2026, 10, 15, 16, 5, tzinfo=ET).timestamp()  # the next session's close
    eng.live_quotes.update("GAMA", 13.0, 20.0, second - 60, "tape")
    assert grade_events(eng, second - 3600) == 0  # not yet
    assert grade_events(eng, second) == 1
    rec = event_record(eng, second)
    assert (rec["n"], rec["realized"], rec["implied"], rec["beat_implied"]) == (
        1,
        35.0,
        25.0,
        1.0,
    )  # moved more than priced
    assert rec["kinds"] == [
        {
            "event": "pdufa",
            "label": "FDA decision",
            "n": 1,
            "realized": 35.0,
            "implied": 25.0,
            "beat_implied": 1.0,
        }
    ]
    assert event_record(semis_engine(tmp_path / "b"), second) == {"n": 0, "kinds": []}
