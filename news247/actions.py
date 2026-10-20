"""Needs you: one ranked list of everything on this phone that wants a decision now.

Every feature that watches something for a phone (tracked calls with a stop and a target, price
alerts, reminders, holdings and the watchlist reporting, divergence and second-chance flags, room
left, the paper autopilot, today's alerts on your stocks, the portfolio's own line) already pushes
or shows its piece somewhere. This reads them all at once and ranks them by how soon they need
you: a stop already hit before a stop 0.6% away, a report tonight on a holding before one tomorrow
on the watchlist. Each item says what happened, how far, in dollars where it can, where to look,
and the one tap that settles it when there is one. An item dismissed stays away for 20 hours.

Only what is known: a stock with no live price is left out rather than guessed at.
"""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .desk import ACT_DONE_S

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
NEAR_PCT = 1.0  # a level this close (%) is "near"
REMIND_AHEAD_S = 2 * 3600.0  # reminders due within this long
AUTO_EXIT_S = 30 * 60.0  # autopilot calls closing within this long
BIG_MOVE = 4.0  # a holding moving this much (%) on the day
MAX_UNREAD = 4  # today's unopened HIGH+ alerts on your stocks, listed one by one


def _usd(v: float) -> str:
    return ("−$" if v < 0 else "$") + f"{abs(v):,.0f}"


def _px(v: float) -> str:
    return f"${v:,.2f}"


def _mins(s: float) -> str:
    m = max(1, round(s / 60))
    return f"{m} min" if m < 60 else f"{m // 60}h {m % 60:02d}m" if m % 60 else f"{m // 60}h"


def _item(key: str, kind: str, urgency: float, icon: str, title: str, detail: str = "", symbol: str = "",
          open_: str = "", do: dict[str, Any] | None = None, ts: float = 0.0) -> dict[str, Any]:  # fmt: skip
    return {"key": key, "kind": kind, "urgency": round(urgency, 1), "icon": icon, "title": title, "detail": detail,
            "symbol": symbol, "open": open_, "do": do, "ts": ts}  # fmt: skip


def action_center(engine: Engine, device: str, now: float | None = None, limit: int = 20) -> dict[str, Any]:
    """This phone's items, most urgent first: {"items", "count", "urgent", "hidden"}."""
    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    px = {}

    def quote(sym: str) -> tuple[float, float, float | None] | None:
        if sym not in px:
            px[sym] = engine.price_of(sym, 6 * 3600, now=now)
        return px[sym]

    items: list[dict[str, Any]] = []
    items += _calls(p, quote, now)
    items += _price_alerts(p, quote)
    items += _reminders(p, now)
    items += _reports(engine, device, p, now)
    mine = set(p.get("watchlist") or []) | set(p.get("holdings") or {})
    touched = set(p.get("opened") or []) | set(p.get("saved") or [])
    items += _flags(engine, mine, touched, now)
    items += _room(engine, mine, now)
    items += _unread(engine, p, mine, now)
    items += _holdings(p, quote, now)
    done = {k: t for k, t in (p.get("act_done") or {}).items() if now - t < ACT_DONE_S}
    shown = [i for i in items if i["key"] not in done]
    shown.sort(key=lambda i: (-i["urgency"], -(i["ts"] or 0)))
    return {
        "items": shown[:limit],
        "count": len(shown),
        "urgent": sum(1 for i in shown if i["urgency"] >= 70),
        "hidden": len(items) - len(shown),
    }


# --------------------------------------------------------------------------- tracked calls


def _calls(p: dict[str, Any], quote: Any, now: float) -> list[dict[str, Any]]:
    out = []
    for t in p.get("tracked") or []:
        sym, long = t["symbol"], t.get("direction", "long") == "long"
        q = quote(sym)
        price = q[1] if q else None
        pnl = None
        if price is not None and t.get("entry"):
            r = (price / t["entry"] - 1) * 100
            pnl = r if long else -r
        now_line = (
            f"now {_px(price)} ({pnl:+.2f}%)" if price is not None and pnl is not None else "no live price"
        )
        side = "long" if long else "short"
        if t.get("auto"):
            left = (t.get("exit_at") or 0) - now
            if 0 < left <= AUTO_EXIT_S:
                out.append(_item(f"auto:{t['id']}", "auto_exit", 40, "🤖", f"Autopilot closes {side} {sym} in {_mins(left)}",
                                 now_line, sym, "#a=desk", ts=t.get("ts", 0)))  # fmt: skip
            continue  # the autopilot closes its own calls at their stop or target
        for which, hit_u, near_u, icon in (("stop", 95, 80, "🛑"), ("target", 85, 65, "🎯")):
            lvl = t.get(which)
            if lvl is None:
                continue
            close = {"label": "Close it", "action": "track_close",
                     "body": {"id": t["id"], "lesson": "stopped out" if which == "stop" else "target reached"}}  # fmt: skip
            if t.get(f"{which}_hit"):
                verb = "hit your stop" if which == "stop" else "reached your target"
                out.append(_item(f"{which}:{t['id']}", f"{which}_hit", hit_u, icon, f"{sym} {verb} ({_px(lvl)})",
                                 f"{side} from {_px(t['entry'])} · {now_line}. Close it, or move the {which}.", sym,
                                 f"#t={sym}", close, t.get(f"{which}_hit") or 0))  # fmt: skip
            elif price is not None:
                dist = abs(price / lvl - 1) * 100
                if dist <= NEAR_PCT:
                    out.append(_item(f"near_{which}:{t['id']}", f"near_{which}", near_u - dist * 5, icon,
                                     f"{sym} is {dist:.1f}% from your {which} ({_px(lvl)})",
                                     f"{side} from {_px(t['entry'])} · {now_line}", sym, f"#t={sym}", ts=now))  # fmt: skip
    return out


# --------------------------------------------------------------------------- price alerts


def _price_alerts(p: dict[str, Any], quote: Any) -> list[dict[str, Any]]:
    out = []
    for a in p.get("price_alerts") or []:
        if a.get("fired"):
            continue
        q = quote(a["symbol"])
        if not q or q[1] is None:
            continue
        price, chg, sym = q[1], q[2], a["symbol"]
        if a["kind"] in ("above", "below"):
            lvl = a["value"]
            dist = (lvl / price - 1) * 100 if a["kind"] == "above" else (price / lvl - 1) * 100
            line = f"{sym} is {dist:.1f}% from your {_px(lvl)} alert"
        elif a["kind"] == "trail" and a.get("peak"):
            lvl = a["peak"] * (1 - a["value"] / 100)
            dist = (price / lvl - 1) * 100
            line = f"{sym} is {dist:.1f}% above its trailing stop ({_px(lvl)})"
        elif a["kind"] == "move" and chg is not None:
            dist = a["value"] - abs(chg)
            line = f"{sym} is {chg:+.1f}% today: {dist:.1f} pts from your ±{a['value']:g}% alert"
        else:
            continue
        if 0 <= dist <= NEAR_PCT:
            out.append(_item(f"pa:{a['id']}", "near_alert", 60 - dist * 10, "🔔", line, f"now {_px(price)}", sym,
                             f"#t={sym}", ts=a.get("created", 0)))  # fmt: skip
    return out


# --------------------------------------------------------------------------- reminders


def _reminders(p: dict[str, Any], now: float) -> list[dict[str, Any]]:
    out = []
    for r in p.get("reminders") or []:
        left = r["due"] - now
        if 0 <= left <= REMIND_AHEAD_S:
            out.append(_item(f"rem:{r['id']}", "reminder", 50 + 20 * (1 - left / REMIND_AHEAD_S), "⏰",
                             f"Reminder in {_mins(left)}", r.get("title", ""), (r.get("tickers") or [""])[0],
                             f"#a={r['alert_id']}", {"label": "Clear", "action": "remind_clear", "body": {"id": r["id"]}},
                             r["due"]))  # fmt: skip
    return out


# --------------------------------------------------------------------------- reports


def _reports(engine: Engine, device: str, p: dict[str, Any], now: float) -> list[dict[str, Any]]:
    from .plans import event_risk

    out = []
    held = set()
    for e in event_risk(engine, device, now, days=1)["events"]:
        held.add(e["symbol"])
        sym, today = e["symbol"], e["in_days"] == 0
        when = (" " + e["when"]) if e.get("when") else ""
        title = (
            f"{sym} reports{when} {'today' if today else 'tomorrow'}"
            if e["kind"] == "earnings"
            else f"{e['title']} {'today' if today else 'tomorrow'}"
        )
        bits = []
        if e.get("implied_usd") is not None:
            bits.append(
                f"options price ±{e['implied_pct']:.1f}% · about ±{_usd(e['implied_usd'])} on your {e['shares']:g} shares"
            )
        elif e.get("usual_usd") is not None:
            bits.append(
                f"its reports usually move ±{e['usual_pct']:.1f}% · about ±{_usd(e['usual_usd'])} on your {e['shares']:g} shares"
            )
        elif e.get("value"):
            bits.append(f"you hold {e['shares']:g} shares ({_usd(e['value'])})")
        out.append(_item(f"earn:{sym}:{e['date']}", "report", 78 if today else 58, "📅", title, " · ".join(bits), sym,
                         f"#t={sym}", ts=now))  # fmt: skip
    book = getattr(getattr(engine, "trends", None), "earnings", None)
    if book is None:
        return out
    day = datetime.fromtimestamp(now, ET).date()
    for sym in p.get("watchlist") or []:
        if sym in held:
            continue
        e = book.lookup(sym, day)
        if not e or e.get("in_days") not in (0, 1):
            continue
        today = e["in_days"] == 0
        when = (" " + e["when"]) if e.get("when") else ""
        out.append(_item(f"earn:{sym}:{e['date']}", "report", 52 if today else 38, "📅",
                         f"{sym} reports{when} {'today' if today else 'tomorrow'}", "on your watchlist", sym,
                         f"#t={sym}", ts=now))  # fmt: skip
    return out


# --------------------------------------------------------------------------- divergence, second chance, room


def _today_log(engine: Engine, key: str, now: float) -> list[dict[str, Any]]:
    day = datetime.fromtimestamp(now, ET).date().isoformat()
    try:
        return [e for e in json.loads(engine.storage.get_setting(key) or "[]") if e.get("day") == day]
    except ValueError:
        return []


def _flags(engine: Engine, mine: set[str], touched: set[str], now: float) -> list[dict[str, Any]]:
    out = []
    for e in _today_log(engine, "diverge_log", now):
        if e["symbol"] not in mine and e["alert_id"] not in touched:
            continue
        a = engine.storage.get_alert(e["alert_id"]) or {}
        good = "good" if e.get("sign", 1) > 0 else "bad"
        out.append(_item(f"dv:{e['alert_id']}", "diverge", 72, "⚠", f"{e['symbol']} is going against its {good} news",
                         f"{e['against']:.1f}% the other way: “{a.get('title', '')[:90]}”", e["symbol"],
                         f"#a={e['alert_id']}", ts=e["ts"]))  # fmt: skip
    for e in _today_log(engine, "retest_log", now):
        if e["symbol"] not in mine and e["alert_id"] not in touched:
            continue
        a = engine.storage.get_alert(e["alert_id"]) or {}
        out.append(_item(f"rt:{e['alert_id']}", "retest", 62, "↩", f"{e['symbol']} is back at its alert price",
                         f"it ran {e['ran']:+.1f}% its way first: “{a.get('title', '')[:90]}”", e["symbol"],
                         f"#a={e['alert_id']}", ts=e["ts"]))  # fmt: skip
    return out


def _room(engine: Engine, mine: set[str], now: float) -> list[dict[str, Any]]:
    from .catalysts import room_left

    if not mine:
        return []
    out = []
    for r in room_left(engine, now, limit=40)["rows"]:
        if r["symbol"] not in mine:
            continue
        out.append(_item(f"room:{r['id']}", "room", 45, "↗", f"{r['symbol']} has room left by its record",
                         f"{r['since']:+.1f}% its way so far, of a typical {r['typical']:+.1f}% ({r['catalyst']}, "
                         f"{round(r['hit'] * 100)}% went their way, {r['n']})", r["symbol"], f"#a={r['id']}",
                         ts=r.get("created") or 0))  # fmt: skip
    return out


# --------------------------------------------------------------------------- today's alerts you haven't opened


def _unread(engine: Engine, p: dict[str, Any], mine: set[str], now: float) -> list[dict[str, Any]]:
    if not mine:
        return []
    day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    opened = set(p.get("opened") or [])
    rows = [
        a
        for a in engine.storage.recent_alerts(300, since=day0)
        if a.get("severity") in ("HIGH", "CRITICAL")
        and a.get("kind") != "system"
        and a["id"] not in opened
        and mine & set(a.get("tickers") or [])
    ]
    rows.sort(key=lambda a: (a.get("severity") != "CRITICAL", -a.get("created", 0)))
    out = []
    for a in rows[:MAX_UNREAD]:
        sym = next(t for t in a.get("tickers") or [] if t in mine)
        mins = (now - a.get("created", now)) / 60
        out.append(_item(f"unread:{a['id']}", "unread", (66 if a["severity"] == "CRITICAL" else 54) - min(mins / 60, 6),
                         "●", a.get("title", "")[:140], f"{a['severity']} · {_mins(max(60, now - a.get('created', now)))} ago · not opened yet",
                         sym, f"#a={a['id']}", ts=a.get("created", 0)))  # fmt: skip
    if len(rows) > MAX_UNREAD:
        out[-1]["detail"] += f" · {len(rows) - MAX_UNREAD} more on the tape (Mine)"
    return out


# --------------------------------------------------------------------------- the portfolio


def _holdings(p: dict[str, Any], quote: Any, now: float) -> list[dict[str, Any]]:
    out = []
    day = datetime.fromtimestamp(now, ET).date().isoformat()
    total = moved = 0.0
    for sym, h in (p.get("holdings") or {}).items():
        q = quote(sym)
        if not q or q[1] is None or not h.get("shares"):
            continue
        value = q[1] * h["shares"]
        total += value
        chg = q[2]
        if chg is None:
            continue
        dpnl = value - value / (1 + chg / 100)
        moved += dpnl
        if abs(chg) >= BIG_MOVE:
            out.append(_item(f"move:{sym}:{day}", "big_move", 58 + min(abs(chg), 10), "▲" if chg > 0 else "▼",
                             f"{sym} {chg:+.1f}% today", f"{'+' if dpnl > 0 else ''}{_usd(dpnl)} on your {h['shares']:g} shares",
                             sym, f"#t={sym}", ts=now))  # fmt: skip
    line = float(((p.get("prefs") or {}).get("port_alert")) or 0)
    prev = total - moved
    if line and prev > 0:
        day_pct = moved / prev * 100
        if abs(day_pct) >= line:
            out.append(_item(f"port:{day}", "portfolio", 68, "◆", f"Your portfolio is {day_pct:+.1f}% today",
                             f"{'+' if moved > 0 else ''}{_usd(moved)} · past your ±{line:g}% line", "", "#a=desk", ts=now))  # fmt: skip
    return out
