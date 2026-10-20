"""What the record learned this week: every verdict the desk changed its mind on, in one place.

The desk's records re-fit every hour: how to enter each kind of news, how to exit it, where its stop
belongs, how often it goes its way by the close, which signals work here. Each changes quietly, so a
trader following last month's playbook never hears that the record moved. Every day the desk now
keeps a snapshot of those verdicts, and once a week it compares today's with the one from a week ago:

- **New on the record:** a kind of news that cleared its bar this week, with its whole playbook
  ("in at the open, out the next day, stop at 3%").
- **A new way in, out, or a new stop:** "Contract wins: exit the next day now (+1.2% on average),
  was at the close."
- **Its odds moved:** a kind's share that went its way by the close moved 8+ points on 15+ graded.
- **A signal changed grade:** "Second legs: strong now (64%, 25), was fair."

The weekly push says it in four lines, and the Watch tab keeps the full list. A change of verdict is
itself only as good as the counts behind it, and they're shown. History, not a forecast.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from .engine import Engine

ET = ZoneInfo("America/New_York")
HIT_MOVE = 0.08  # a kind's close hit rate must move this much...
HIT_N = 15  # ...on this many graded, both weeks, to count as news
MAX_ITEMS = 12
RANK = {"new": 0, "entry": 1, "exit": 2, "stop": 3, "odds": 4, "signal": 5}


def snapshot(engine: Engine) -> dict[str, Any]:
    """The verdicts as they stand: per kind of news and per signal."""
    kinds: dict[str, Any] = {}
    from .holdout import use

    defaults = {"exit": "close", "entry": "alert", "stop": "none"}
    for name, field in (("exit_rates", "exit"), ("entry_rates", "entry"), ("stop_rates", "stop")):
        for k, r in (getattr(engine, name, None) or {}).items():
            if k == "*" or not r.get("best"):
                continue
            row = kinds.setdefault(k, {"label": r["label"]})
            pick = use(r, defaults[field]) or r["best"]  # the default when the pick didn't hold (holdout.py)
            if pick not in r["styles"]:
                continue
            row[field] = pick
            row[f"{field}_avg"] = r["styles"][pick]["avg"]
            if field == "stop" and r.get("width") and pick == r["best"]:
                row["width"] = r["width"]
            if field == "exit" and "close" in r["styles"]:
                row["hit"], row["n"] = r["styles"]["close"]["hit"], r["styles"]["close"]["n"]
    sigs = {}
    try:
        from .signals import scoreboard

        for r in scoreboard(engine)["rows"]:
            sigs[r["key"]] = {"label": r["label"], "verdict": r["verdict"], "hit": r["hit"], "n": r["n"]}
    except Exception:  # noqa: BLE001 - a snapshot without signals still says what the kinds learned
        pass
    return {"kinds": kinds, "signals": sigs}


def _key(day: Any) -> str:
    return f"learned:{day.isoformat()}"


def save_snapshot(engine: Engine, now: float | None = None) -> None:
    """Today's verdicts, kept under today's date (the last save of the day wins)."""
    day = datetime.fromtimestamp(now or time.time(), ET).date()
    engine.storage.set_setting(_key(day), json.dumps(snapshot(engine), separators=(",", ":")))


def previous(
    engine: Engine, now: float | None = None, days: int = 7
) -> tuple[dict[str, Any] | None, str | None]:
    """The snapshot from ``days`` ago (or the nearest older one, up to a week further; else the nearest newer one, 5+ days old)."""
    today = datetime.fromtimestamp(now or time.time(), ET).date()
    for back in [*range(days, days + 8), *range(days - 1, 4, -1)]:
        day = today - timedelta(days=back)
        raw = engine.storage.get_setting(_key(day))
        if raw:
            try:
                return json.loads(raw), day.isoformat()
            except ValueError:
                continue
    return None, None


def _names() -> dict[str, dict[str, str]]:
    from .entries import STYLES as IN
    from .exits_kind import STYLES as OUT
    from .stops_kind import STYLES as STOP

    return {"entry": IN, "exit": OUT, "stop": STOP}


def _stop_words(row: dict[str, Any], names: dict[str, str]) -> str:
    if row.get("stop") == "none":
        return "no stop"
    if row.get("width"):
        return f"a {row['width']:.1f}% stop"
    return names.get(row.get("stop", ""), "").lower()


def diff(old: dict[str, Any], new: dict[str, Any]) -> list[dict[str, Any]]:
    """What changed from ``old`` to ``new``, most important first: [{"what", "key", "label", "text"}]."""
    names = _names()
    verbs = {"entry": "enter", "exit": "exit", "stop": "stop"}
    out: list[dict[str, Any]] = []
    ok, nk = old.get("kinds") or {}, new.get("kinds") or {}
    for k, r in nk.items():
        o = ok.get(k)
        if not o:
            bits = []
            if r.get("entry"):
                bits.append(f"in {names['entry'][r['entry']].lower()}")
            if r.get("exit"):
                bits.append(f"out {names['exit'][r['exit']].lower()}")
            if r.get("stop"):
                bits.append(_stop_words(r, names["stop"]))
            graded = (
                f" ({r['n']} graded, {round(r['hit'] * 100)}% its way by the close)" if r.get("n") else ""
            )
            out.append({"what": "new", "key": k, "label": r["label"],
                        "text": f"New on the record: {r['label'].lower()}{graded}" + (f": {', '.join(bits)}." if bits else ".")})  # fmt: skip
            continue
        for field in ("entry", "exit", "stop"):
            was, now = o.get(field), r.get(field)
            if not now or not was or (was == now and (field != "stop" or o.get("width") == r.get("width"))):
                continue
            if field == "stop":
                text = f"{r['label']}: {_stop_words(r, names['stop'])} now ({r['stop_avg']:+.1f}% on average), was {_stop_words(o, names['stop'])}."
            else:
                text = f"{r['label']}: {verbs[field]} {names[field][now].lower()} now ({r[field + '_avg']:+.1f}% on average), was {names[field][was].lower()}."
            out.append({"what": field, "key": k, "label": r["label"], "text": text})
        if (
            r.get("hit") is not None
            and o.get("hit") is not None
            and min(r.get("n", 0), o.get("n", 0)) >= HIT_N
            and abs(r["hit"] - o["hit"]) >= HIT_MOVE - 1e-9
        ):
            out.append({"what": "odds", "key": k, "label": r["label"],
                        "text": f"{r['label']} went their way {round(r['hit'] * 100)}% by the close, from {round(o['hit'] * 100)}% ({r['n']} graded)."})  # fmt: skip
    osig, nsig = old.get("signals") or {}, new.get("signals") or {}
    for k, r in nsig.items():
        was = (osig.get(k) or {}).get("verdict", "thin")
        if r["verdict"] == was or r["verdict"] == "thin":
            continue
        rec = f"{round(r['hit'] * 100)}%, {r['n']}" if r.get("hit") is not None else f"{r['n']}"
        tail = "new on the record" if was == "thin" else f"was {was}"
        out.append(
            {
                "what": "signal",
                "key": k,
                "label": r["label"],
                "text": f"{r['label']}: {r['verdict']} now ({rec}), {tail}.",
            }
        )
    out.sort(key=lambda x: (RANK[x["what"]], x["label"]))
    return out[:MAX_ITEMS]


def learned(engine: Engine, now: float | None = None, days: int = 7) -> dict[str, Any]:
    """What changed since the snapshot a week ago: {"since", "items"}; "since" is None until one exists."""
    base, since = previous(engine, now, days)
    if base is None:
        return {"since": None, "items": [], "days": days}
    return {"since": since, "items": diff(base, snapshot(engine)), "days": days}


def push(r: dict[str, Any], now: float) -> dict[str, Any] | None:
    if not r.get("items"):
        return None
    items = r["items"]
    lines = [f"• {x['text']}" for x in items[:4]] + (
        [f"+ {len(items) - 4} more in the app"] if len(items) > 4 else []
    )
    return {"id": f"lw-{int(now)}", "title": f"🧠 What the record learned this week ({len(items)})",
            "body": "\n".join(lines)[:700], "severity": "MEDIUM", "kind": "system", "tickers": [],
            "tag": "lw", "ts": now, "open": "/app/#learned"}  # fmt: skip
