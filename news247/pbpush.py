"""The playbook on the lock screen: a push when the playbook's way in comes, and when it's out.

The playbook today (today.py) shows, live, where the playbook stands on every alert. This sends the
two moments that matter to the phones that care about the stock (the alert reached it, or it
watches, holds or has a call on the stock), once each:

- **In:** the playbook's way in just came, for any way in but the alert itself (that one is the
  alert's own push): "🧭 ACME: the playbook's way in, on the second leg at $102.00 · out the next
  day · its pair made +1.4% on average here (12)".
- **Out:** the playbook's way out, when it isn't simply the close after an entry at the alert (that's
  just the bell): "🏁 ACME: out the next day, +2.1% on the playbook (the plain way +1.0%)".

Each counts against the phone's hourly budget for signal pushes (pushbudget.py: what's over it
folds into the half-hourly digest), and Settings → *The playbook's moves* switches them off. Paper
only: the push says what the playbook did, not an order.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

KEY = "pb_push_seen"
KEEP = 8 * 86400


def _payload(row: dict[str, Any], what: str, avg: dict[str, Any] | None, now: float) -> dict[str, Any]:
    sym = row["symbol"]
    pair = row["pair"]
    way_in, way_out = (pair.split(", out ") + [""])[:2]
    if what == "in":
        title = f"🧭 {sym}: the playbook's way in, {way_in[:1].lower() + way_in[1:]}" + (
            f" at ${row['entry']:,.2f}" if row.get("entry") else ""
        )
        rec = f" · its pair made {avg['avg']:+.1f}% on average here ({avg['n']})" if avg else ""
        body = f"Out {way_out}{rec}\n{row['title']}"
    else:
        title = f"🏁 {sym}: out {way_out}, {row['pnl']:+.1f}% on the playbook" + (
            f" (the plain way {row['plain']:+.1f}%)" if row.get("plain") is not None else ""
        )
        body = f"{pair}{(' ' + row['why']) if row.get('why') else ''}\n{row['title']}"
    return {"id": f"pb-{what}-{row['id']}"[:60], "title": title[:140], "body": body[:500], "severity": "MEDIUM",
            "kind": "system", "tickers": [sym], "tag": f"pb-{row['id']}", "ts": now, "open": f"/app/#a={row['id']}"}  # fmt: skip


async def send_moves(engine: Engine, now: float | None = None) -> int:
    """Push each playbook entry (but at the alert) and each exit once, to the phones on the stock."""
    from .today import board
    from .waits import _devices

    now = now or time.time()
    if engine.webpush is None:
        return 0
    seen: dict[str, float] = {
        k: t for k, t in json.loads(engine.storage.get_setting(KEY) or "{}").items() if now - t < KEEP
    }
    sent, first = 0, not seen and engine.storage.get_setting(KEY) is None
    for row in board(engine, now)["rows"]:
        moves = []
        if row["status"] in ("in", "out") and row["in"] != "alert":
            moves.append("in")
        if (
            row["status"] == "out"
            and row.get("pnl") is not None
            and (row["in"], row["out"]) != ("alert", "close")
        ):
            moves.append("out")  # the plain pair's exit is just the bell: no push for it
        for what in moves:
            k = f"{row['id']}:{what}"
            if k in seen:
                continue
            seen[k] = now
            if first:
                continue  # the first run after an upgrade: everything already happened, nothing to push
            a = engine.storage.get_alert(row["id"]) or {"id": row["id"]}
            cr = (a.get("edge") or {}).get("combo_rec") or {}
            avg = (cr.get("styles") or {}).get(cr.get("best") or "")
            payload = _payload(row, what, avg, now)
            for dev in sorted(_devices(engine, a, row["symbol"], "pb_push")):
                if await engine._to_device(dev, dict(payload), urgency="normal"):
                    sent += 1
    engine.storage.set_setting(KEY, json.dumps(seen))
    return sent
