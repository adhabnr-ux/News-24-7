"""A budget for signal pushes: the lock screen stays readable as the signals multiply.

Beyond the alerts, the desk now pushes live signals:
- the wait is over;
- the mainstream caught up;
- a second leg;
- volume 15 minutes in;
- the call turning;
- a run-up into an event;
- the book on a market-wide headline.
On a busy morning they can stack up. Each phone gets a budget (Settings → Signal pushes: 4, 8 or 12
an hour, or no limit; 8 by default). A signal over the budget isn't lost: it's folded, and a digest
push every half hour lists what was folded ("🗂 3 more signals: ACME second leg · BETA catch-up ·
…"), each still on its alert's sheet. Alerts themselves and price alerts are never folded here:
they have their own controls (attention budget, burst protection, digest).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .engine import Engine

PREFIXES = ("wt-", "cu-", "l2-", "vc-", "cn-", "ru-", "bm-")  # the signal pushes, by their id
CHOICES = (0, 4, 8, 12)  # 0: no limit
DEFAULT = 8
WINDOW = 3600
DIGEST_EVERY = 1800


def is_signal(payload: dict[str, Any]) -> bool:
    return str(payload.get("id") or "").startswith(PREFIXES)


def cap(engine: Engine, device: str) -> int:
    v = ((engine.desk.profiles.get(device) or {}).get("prefs") or {}).get("signal_cap", DEFAULT)
    return int(v) if v in CHOICES else DEFAULT


def admit(engine: Engine, device: str, payload: dict[str, Any], now: float | None = None) -> bool:
    """True to push it now; False when it's over this phone's budget (folded for the digest)."""
    if not is_signal(payload):
        return True
    now = now or time.time()
    p = engine.desk.profiles.get(device)
    if p is None:
        return True
    limit = cap(engine, device)
    sent = [t for t in p.get("signal_sent") or [] if now - t < WINDOW]
    if limit and len(sent) >= limit:
        folded = p.setdefault("signal_folded", [])
        folded.append(
            {"ts": now, "title": str(payload.get("title") or "")[:120], "open": payload.get("open") or ""}
        )
        p["signal_folded"] = folded[-40:]
        p["signal_sent"] = sent
        return False
    p["signal_sent"] = [*sent, now]
    return True


async def send_digest(engine: Engine, now: float | None = None) -> int:
    """Every half hour: one push per phone listing the signals its budget folded."""
    now = now or time.time()
    sent = 0
    for dev, p in list(engine.desk.profiles.items()):
        folded = p.get("signal_folded") or []
        if not folded or now - p.get("signal_digest_at", 0) < DIGEST_EVERY:
            continue
        p["signal_folded"], p["signal_digest_at"] = [], now
        heads = [f["title"] for f in folded]
        payload = {"id": f"sd-{int(now)}", "title": f"🗂 {len(heads)} more signal{'s' if len(heads) != 1 else ''} while your budget was full",
                   "body": " · ".join(h.split(":")[0] for h in heads)[:400], "severity": "MEDIUM", "kind": "system", "tickers": [],
                   "tag": "signal-digest", "ts": now, "open": "/app/#a=desk"}  # fmt: skip
        if await engine._to_device(dev, payload, urgency="normal"):
            sent += 1
        engine.desk.save(dev)
    return sent
