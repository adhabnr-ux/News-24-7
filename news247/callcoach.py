"""The playbook coaches your open calls: its live verdicts, applied to the calls you opened yourself.

The exit rules the playbook learned (its way out for each kind of news, the stall rule, the give-back
rule for overnight holds, denials) all ran for the paper autopilot only. Your own paper calls from
alerts now get them too, as items in Needs you (the action center), each with a one-tap close:

- **Its way out came:** the playbook's way out for this kind of news is due now: an hour in, at the
  close, the next day, five sessions on.
- **Stalled:** 30 minutes in it's gone nowhere, and stalled alerts like it lost the rest of the day
  here (stall.py).
- **Gave back its run, or an event tonight:** before an overnight hold, it gave back the kind of run
  that lost overnight here (giveback.py), or its stock reports tonight (eventrisk.py).
- **Denied:** a later headline denied or walked back its news (denials.py).

The same verdicts come as one push each (Settings → *Coach my calls*), on the signal budget. They are
the playbook's record, not an order: closing is always your tap. Paper only.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

KEY = "coach_sent"
OUT_WORDS = {"h1": "an hour in", "close": "at the close", "d1": "the next day", "d5": "five sessions on"}


def verdicts(engine: Engine, device: str, now: float | None = None) -> list[dict[str, Any]]:
    """[{"call", "kind", "title", "detail", "urgency"}] for this phone's own open calls from alerts."""
    from .autopilot import exit_time, playbook_out
    from .desk import session_edge
    from .giveback import live as gb_live
    from .stall import live as stall_live

    now = now or time.time()
    p = engine.desk.profiles.get(device) or {}
    out = []
    for t in p.get("tracked") or []:
        if t.get("auto") or not t.get("alert_id"):
            continue
        a = engine.storage.get_alert(t["alert_id"])
        if not a:
            continue
        e = a.get("edge") or {}
        sym = t["symbol"]
        ref = (e.get("refs") or {}).get(sym)
        q = engine.price_of(sym, 900, now=now)
        live = (q[1] / ref - 1) * 100 if ref and q and q[1] else None
        created = float(a["created"])
        if e.get("denied"):
            out.append({"call": t, "kind": "denied", "urgency": 82, "title": f"{sym}: its news was denied",
                        "detail": f"“{e['denied'].get('title', '')}”: the playbook drops calls on denied news"})  # fmt: skip
            continue
        if 1800 <= now - created <= 3600 and live is not None:
            sv = stall_live(engine, a, live)
            if sv and sv["exit"]:
                out.append(
                    {
                        "call": t,
                        "kind": "stall",
                        "urgency": 70,
                        "title": f"{sym} stalled 30 minutes in",
                        "detail": sv["line"],
                    }
                )
                continue
        close_ts = session_edge("close", created - 31, engine.calendar)
        pout = playbook_out(a)
        if pout in ("d1", "d5") and 0 < close_ts - now <= 900 and live is not None:
            from .eventrisk import verdict as event_verdict

            gv = event_verdict(engine, a, sym)  # its stock reports tonight
            if not gv or not gv["exit"]:
                gv = gb_live(engine, a, live)
            if gv and gv["exit"]:
                out.append({"call": t, "kind": "giveback", "urgency": 74, "title": f"{sym}: out at the close instead of overnight?",
                            "detail": gv["line"]})  # fmt: skip
                continue
        if pout in OUT_WORDS:
            due = exit_time(pout, created, engine.calendar)
            if now >= due - 300:
                cr = e.get("combo_rec") or {}
                name = cr.get("name") or OUT_WORDS[pout]
                out.append({"call": t, "kind": "exit", "urgency": 64, "title": f"{sym}: the playbook's way out is {OUT_WORDS[pout]}",
                            "detail": f"{name} is how {(cr.get('label') or 'this kind of news').lower()} has paid best here"})  # fmt: skip
    return out


def items(engine: Engine, device: str, now: float | None = None) -> list[dict[str, Any]]:
    """The verdicts as Needs-you items, each with a one-tap close."""
    from .actions import _item

    res = []
    for v in verdicts(engine, device, now):
        t = v["call"]
        close = {
            "label": "Close it",
            "action": "track_close",
            "body": {"id": t["id"], "lesson": f"coach: {v['kind']}"},
        }
        res.append(_item(f"coach:{v['kind']}:{t['id']}", f"coach_{v['kind']}", v["urgency"], "🧭", v["title"], v["detail"],
                         t["symbol"], f"#a={t['alert_id']}", close))  # fmt: skip
    return res


async def send(engine: Engine, now: float | None = None) -> int:
    """One push per call and verdict, to the phone that holds the call."""
    now = now or time.time()
    if engine.webpush is None:
        return 0
    sent_keys: dict[str, float] = {
        k: v for k, v in json.loads(engine.storage.get_setting(KEY) or "{}").items() if now - v < 8 * 86400
    }
    sent = 0
    for dev, p in list(engine.desk.profiles.items()):
        if not (p.get("prefs") or {}).get("coach_calls", True) or not p.get("tracked"):
            continue
        for v in verdicts(engine, dev, now):
            t = v["call"]
            k = f"{dev}:{t['id']}:{v['kind']}"
            if k in sent_keys:
                continue
            sent_keys[k] = now
            payload = {"id": f"cc-{t['id']}-{v['kind']}"[:60], "title": f"🧭 {v['title']}", "body": v["detail"][:400],
                       "severity": "HIGH" if v["urgency"] >= 70 else "MEDIUM", "kind": "system", "tickers": [t["symbol"]],
                       "tag": f"cc-{t['id']}", "ts": now, "open": f"/app/#a={t['alert_id']}"}  # fmt: skip
            if await engine._to_device(dev, payload, urgency="normal"):
                sent += 1
    engine.storage.set_setting(KEY, json.dumps(sent_keys))
    return sent
