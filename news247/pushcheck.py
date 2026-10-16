"""Is my phone getting what it should? Per phone: why one alert did (or didn't) reach it, the log of
its last pushes, and an "is it working?" check from the news feeds down to its lock screen.

Everything here reads what the server already knows (the push log in notify/channels.py, the desk's
settings, the sources' health). Nothing is guessed: when the log has the answer it says so; when it
doesn't (the alert is older than the log), the answer is worked out from the rules, with the
phone's settings as they are now, and says that too.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from .models import Severity

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")

STATUS_TEXT = {
    "sent": "reached this phone",
    "failed": "the push service turned it down",
    "gone": "this phone's subscription had expired",
    "held": "kept for your digest",
    "filtered": "kept back by your settings",
}


def _clock(ts: float) -> str:
    d = datetime.fromtimestamp(ts, ET)
    return d.strftime("%-I:%M %p ET")


def phone_floor(engine: Engine) -> Severity | None:
    """The lowest severity the server pushes to every phone (None: web push isn't on)."""
    ch = next((c for c in engine.dispatcher.channels if c.name == "webpush"), None)
    if ch is None:
        return None
    return engine.dispatcher.phone_min or ch.min_severity


def _subs(engine: Engine, device: str) -> list[Any]:
    wp = engine.webpush
    return [s for s in wp.subs.values() if s.device == device] if wp is not None else []  # type: ignore[attr-defined]


def _log(engine: Engine, device: str) -> list[dict[str, Any]]:
    wp = engine.webpush
    return list(wp.log.get(device) or []) if wp is not None else []  # type: ignore[attr-defined]


def _watch_path(engine: Engine, device: str, alert: dict[str, Any]) -> str:
    """Why a MEDIUM alert would go to this phone: "ACME, which you watch" / "a theme you follow"."""
    p = engine.desk.profiles.get(device) or {}
    mine = set(p.get("watchlist") or []) | set(p.get("holdings") or {})
    tickers = [t for t in (alert.get("tickers") or [])[:6] if t]
    hit = [t for t in tickers if t in mine]
    if hit:
        return f"{hit[0]}, which you {'hold' if hit[0] in (p.get('holdings') or {}) else 'watch'}"
    follow = p.get("follow") or {}
    themes = ((alert.get("analysis") or {}).get("themes")) or []
    if set(themes) & set(follow.get("themes") or []):
        return "a theme you follow"
    secs = {li.sector for li in (engine.universe.get(t) for t in tickers) if li is not None and li.sector}
    if secs & set(follow.get("sectors") or []):
        return "a sector you follow"
    return ""


def why_push(engine: Engine, device: str, alert_id: str, now: float | None = None) -> dict[str, Any] | None:
    """Did this alert reach this phone, and if not, the first thing that stopped it, with the one
    tap that would change it. ``steps`` is the whole path, gate by gate."""
    now = now or time.time()
    a = engine.storage.get_alert(alert_id)
    if a is None:
        return None
    created = float(a.get("created") or now)
    sev_name = str(a.get("severity") or "LOW")
    try:
        sev = Severity.parse(sev_name)
    except KeyError:
        sev = Severity.LOW
    tickers = [t for t in a.get("tickers") or [] if t]
    steps: list[dict[str, Any]] = []
    fix: dict[str, Any] | None = None
    blocked = ""

    def gate(ok: bool | None, text: str, fx: dict[str, Any] | None = None) -> None:
        nonlocal fix, blocked
        steps.append({"ok": ok, "text": text})
        if ok is False and not blocked:
            blocked, fix = text, fx

    wp = engine.webpush
    floor = phone_floor(engine)
    if wp is None or floor is None:
        gate(False, "This server doesn't send phone alerts (web push is off)")
    else:
        subs = _subs(engine, device)
        if not subs:
            gate(False, "This phone hasn't turned on alerts", {"label": "Turn on alerts", "app": "push-on"})
        elif min(s.created or 0 for s in subs) > created:
            gate(
                False,
                f"This phone turned alerts on at {_clock(min(s.created for s in subs))}, after it came in",
            )
        else:
            gate(True, "This phone has alerts on")
        if sev >= floor:
            gate(True, f"It's {sev.name}: every phone gets {floor.name} and up")
        elif sev == Severity.MEDIUM and _watch_path(engine, device, a):
            prefs = (engine.desk.profiles.get(device) or {}).get("prefs") or {}
            if prefs.get("watch_push", True):
                gate(True, f"It's MEDIUM, but it's about {_watch_path(engine, device, a)}")
            else:
                gate(False, "You turned off pushes for MEDIUM alerts on your stocks",
                     {"label": "Push my stocks' MEDIUM alerts", "action": "prefs", "body": {"watch_push": True}})  # fmt: skip
        elif sev == Severity.MEDIUM:
            fx = (
                {"label": f"Watch {tickers[0]}", "action": "watch_add", "body": {"symbol": tickers[0]}}
                if tickers
                else None
            )
            gate(False, f"It's MEDIUM: phones get {floor.name} and up, plus MEDIUM alerts on stocks you watch"
                 + (f" ({tickers[0]} isn't one)" if tickers else ""), fx)  # fmt: skip
        else:
            gate(False, f"It's {sev.name}: phones get {floor.name} and up")
        b = engine.desk.block(device, a, created)
        if b:
            gate(False, f"Your settings: {b['text']}", b["fix"])
        else:
            gate(True, "Nothing in your settings holds it back")
        prefs = (engine.desk.profiles.get(device) or {}).get("prefs") or {}
        if prefs.get("digest") and sev != Severity.CRITICAL:
            gate(None, "You're in digest mode: it goes into your next digest instead of buzzing")
        if engine.dispatcher.paused:
            gate(False, "Pushes are paused on the server right now")

    entry = next((e for e in _log(engine, device) if e["id"] == alert_id), None)
    if entry is not None:
        when = _clock(entry["ts"])
        got = entry["status"] == "sent"
        verdict = {
            "sent": f"It reached this phone at {when}.",
            "held": f"It was kept for your digest at {when}.",
            "filtered": f"Your settings kept it back at {when}: {entry['detail']}.",
            "failed": f"The push service turned it down at {when}: {entry['detail']}.",
            "gone": f"At {when} this phone's subscription had expired: turn alerts on again.",
        }.get(entry["status"], STATUS_TEXT.get(entry["status"], entry["status"]))
        if entry["status"] in ("failed", "gone"):
            fix = {"label": "Turn alerts on again", "app": "push-on"}
        elif entry["status"] == "filtered":  # what holds it back now, if anything still does
            b = engine.desk.block(device, a, entry["ts"])
            fix = b["fix"] if b else None
        elif entry["status"] == "held":
            fix = {"label": "Push alerts as they come", "action": "prefs", "body": {"digest": 0}}
        else:
            fix = None
    elif blocked:
        got, verdict = False, blocked + "."
    else:
        got = None
        verdict = (
            "Nothing here would have stopped it, but it isn't in this phone's push log (the log keeps "
            f"the last {getattr(wp, 'LOG_SIZE', 40)}). If it never showed, check that notifications for "
            "this app are allowed in your phone's settings, and any Focus mode."
        )
    return {
        "id": alert_id,
        "title": a.get("title", ""),
        "severity": sev.name,
        "created": created,
        "got": got,
        "verdict": verdict,
        "logged": entry,
        "steps": steps,
        "fix": fix,
        "settings_now": entry is None,  # worked out from the settings as they are now, not logged
    }


def push_log(engine: Engine, device: str, now: float | None = None) -> dict[str, Any]:
    """This phone's last pushes, newest first, and today's tally by outcome."""
    now = now or time.time()
    rows = _log(engine, device)
    day0 = datetime.fromtimestamp(now, ET).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    today: dict[str, int] = {}
    for r in rows:
        if r["ts"] >= day0:
            today[r["status"]] = today.get(r["status"], 0) + 1
    return {"rows": rows, "today": today, "size": getattr(engine.webpush, "LOG_SIZE", 40)}


def phone_health(engine: Engine, device: str, now: float | None = None) -> dict[str, Any]:
    """Is it working? Each link from the news feeds to this phone's lock screen, green or not,
    with what to do about the ones that aren't."""
    now = now or time.time()
    checks: list[dict[str, Any]] = []

    def check(key: str, ok: bool | None, title: str, text: str, fix: dict[str, Any] | None = None) -> None:
        checks.append({"key": key, "ok": ok, "title": title, "text": text, "fix": fix})

    srcs = [s.describe() for s in engine.sources if s.enabled]
    good = [s for s in srcs if s["status"] in ("ok", "starting")]
    bad = [s["name"] for s in srcs if s["status"] not in ("ok", "starting")]
    last_item = max((s.get("last_item_at") or 0 for s in srcs), default=0)
    if not srcs:
        check("feeds", False, "News feeds", "No news sources are running.")
    else:
        share = len(good) / len(srcs)
        txt = f"{len(good)} of {len(srcs)} sources reading fine"
        if last_item:
            txt += f"; the newest headline came in {_ago(now - last_item)} ago"
        if bad:
            txt += f". Not reading: {', '.join(bad[:3])}" + (
                f" and {len(bad) - 3} more" if len(bad) > 3 else ""
            )
        check("feeds", share >= 0.6, "News feeds", txt + ".")

    phase = engine.calendar.market_status(now).get("phase", "")
    quotes = engine.live_quotes.quotes if engine.live_quotes is not None else {}
    newest = max((q.ts for q in quotes.values()), default=0)
    if phase == "closed":
        check("prices", None, "Prices", "The market is closed: prices pick up again at the next session.")
    elif not newest:
        check("prices", False, "Prices", "No live prices yet.")
    else:
        age = now - newest
        check(
            "prices",
            age < 300,
            "Prices",
            f"The newest price is {_ago(age)} old ({len(quotes)} stocks quoted).",
        )

    floor = phone_floor(engine)
    if floor is None:
        check("server", False, "Phone alerts on the server", "Web push isn't turned on for this server.")
    elif engine.dispatcher.paused:
        check(
            "server",
            False,
            "Phone alerts on the server",
            "Pushes are paused (text “resume” to turn them back on).",
        )
    else:
        check("server", True, "Phone alerts on the server", f"On: every phone gets {floor.name} and up.")

    subs = _subs(engine, device)
    if not subs:
        check(
            "phone",
            False,
            "This phone",
            "Alerts aren't turned on on this phone.",
            {"label": "Turn on alerts", "app": "push-on"},
        )
    else:
        fails = max(s.failures for s in subs)
        last_ok = max((s.last_ok or 0) for s in subs)
        txt = f"Alerts on since {datetime.fromtimestamp(min(s.created for s in subs), ET):%b %-d}"
        txt += (
            f"; the last push landed {_ago(now - last_ok)} ago" if last_ok else "; nothing pushed to it yet"
        )
        if fails:
            txt += f"; {fails} failed in a row"
        check("phone", fails < 3, "This phone", txt + ".",
              None if fails < 3 else {"label": "Turn alerts on again", "app": "push-on"})  # fmt: skip

    log = _log(engine, device)
    if log:
        e = log[0]
        check("last", e["status"] in ("sent", "held", "filtered"), "Last push",
              f"{e['title'][:80]}: {STATUS_TEXT.get(e['status'], e['status'])} at {_clock(e['ts'])}"
              + (f" ({e['detail']})" if e.get("detail") else "") + ".")  # fmt: skip

    p = engine.desk.profiles.get(device) or {}
    prefs = p.get("prefs") or {}
    from .desk import in_quiet

    holds = []
    if in_quiet(prefs.get("quiet"), now):
        holds.append("it's your quiet hours (only CRITICAL comes through)")
    snoozed = [c for c, u in (p.get("snooze") or {}).items() if u > now]
    if snoozed:
        holds.append(f"{', '.join(snoozed)} snoozed")
    off = [c for c, on in (prefs.get("cats") or {}).items() if not on]
    if off:
        holds.append(f"{', '.join(off)} turned off")
    muted = [s for s, u in (p.get("muted") or {}).items() if u > now]
    if muted:
        holds.append(f"{len(muted)} stock{'s' if len(muted) != 1 else ''} muted")
    if prefs.get("min_sev"):
        holds.append(f"only {prefs['min_sev']} and up")
    if prefs.get("digest"):
        holds.append("digest mode")
    check("settings", None if holds else True, "Your settings",
          ("Holding some back: " + "; ".join(holds) + ".") if holds else "Nothing held back.")  # fmt: skip

    worst = [c for c in checks if c["ok"] is False]
    return {
        "ok": not worst,
        "summary": "Everything's working." if not worst else f"{worst[0]['title']}: {worst[0]['text']}",
        "checks": checks,
        "market": phase,
    }


def _ago(s: float) -> str:
    s = max(0.0, s)
    if s < 90:
        return f"{int(s)}s"
    if s < 5400:
        return f"{int(s // 60)} min"
    if s < 172800:
        return f"{s / 3600:.0f}h"
    return f"{s / 86400:.0f} days"
