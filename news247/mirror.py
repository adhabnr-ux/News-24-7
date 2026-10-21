"""The broker mirror: the playbook autopilot's paper calls, placed as orders in an Alpaca *paper* account.

Every number on the desk assumes a paper fill at the price on screen. Real orders don't fill there:
there's a spread, a queue, a gap before the open. The mirror measures it. With Alpaca keys set (the
same ALPACA_KEY / ALPACA_SECRET the options feed uses) and the mirror switched on from one phone
(Settings → *Mirror to Alpaca paper*), each call that phone's playbook autopilot opens is sent as a
market order to the Alpaca **paper** account, and closed with the opposite order when the desk closes
it. Then, per call:

- **the entry's cost:** the fill against the desk's paper entry, in the call's direction (+ = the
  fill was worse: paid up on a long, sold lower on a short);
- **the exit's cost**, the same way;
- **the broker's result** against the desk's.

Summed by liquidity band and kind of news, that's what the paper record would have cost in real
fills. It only ever talks to ``paper-api.alpaca.markets``: the live trading host is refused in code,
whatever the configuration says, and live keys don't work on the paper host. Paper only, always.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from .engine import Engine

PAPER = "https://paper-api.alpaca.markets"
LIVE_HOSTS = frozenset({"api.alpaca.markets"})  # never: the mirror is paper only
DEVICE_KEY = "mirror_device"
LOG_KEY = "mirror_log"
KEEP = 500  # mirrored calls kept in the log
FRESH_S = 600  # only calls opened in the last 10 minutes are mirrored (not old ones at switch-on)
FINAL = frozenset({"filled", "canceled", "expired", "rejected", "done_for_day"})


class MirrorError(Exception):
    pass


def _url(path: str) -> str:
    url = PAPER.rstrip("/") + path
    host = urlsplit(url).hostname or ""
    if host in LIVE_HOSTS or "paper" not in host and not host.startswith("127.") and host != "localhost":
        raise MirrorError(f"the mirror only trades on Alpaca paper, not {host}")
    return url


def _headers(engine: Engine) -> dict[str, str]:
    o = engine.cfg.options
    if not (o.alpaca_key and o.alpaca_secret):
        raise MirrorError("no Alpaca keys (ALPACA_KEY and ALPACA_SECRET)")
    return {"APCA-API-KEY-ID": o.alpaca_key, "APCA-API-SECRET-KEY": o.alpaca_secret}


async def _call(engine: Engine, method: str, path: str, **kw: Any) -> Any:
    from .http import HTTPError

    try:
        resp = await engine.http.request(method, _url(path), headers=_headers(engine), timeout_s=10, **kw)
    except HTTPError as e:
        raise MirrorError(f"Alpaca paper said {e.status}: {e.body[:160]}") from e
    return resp.json() if resp.body else {}


def device(engine: Engine) -> str:
    return engine.storage.get_setting(DEVICE_KEY) or ""


def log(engine: Engine) -> dict[str, dict[str, Any]]:
    return json.loads(engine.storage.get_setting(LOG_KEY) or "{}")


def _save(engine: Engine, lg: dict[str, dict[str, Any]]) -> None:
    rows = sorted(lg.items(), key=lambda kv: -kv[1].get("opened", 0))[:KEEP]
    engine.storage.set_setting(LOG_KEY, json.dumps(dict(rows)))


async def account(engine: Engine) -> dict[str, Any]:
    """The paper account's headline numbers (and a check that the keys are paper keys)."""
    a = await _call(engine, "GET", "/v2/account")
    return {
        k: a.get(k) for k in ("status", "equity", "cash", "buying_power", "currency", "pattern_day_trader")
    }


async def switch(engine: Engine, dev: str, on: bool) -> dict[str, Any]:
    """Mirror this phone's playbook autopilot (one phone at a time: the account is the server's)."""
    if not on:
        if device(engine) == dev:
            engine.storage.set_setting(DEVICE_KEY, "")
        return {"on": False}
    acct = await account(engine)  # refuses without keys, or with keys that aren't paper keys
    engine.storage.set_setting(DEVICE_KEY, dev)
    return {"on": True, "account": acct}


def _playbook_rules(p: dict[str, Any]) -> set[str]:
    return {r["id"] for r in p.get("autopilots") or [] if r.get("follow") == "playbook"}


def _order(sym: str, qty: int, side: str, cid: str) -> dict[str, Any]:
    return {"symbol": sym.replace("-", "."), "qty": str(qty), "side": side, "type": "market", "time_in_force": "day",
            "client_order_id": cid}  # fmt: skip


def _leg(o: dict[str, Any]) -> dict[str, Any]:
    px = o.get("filled_avg_price")
    return {"status": o.get("status") or "sent", "order": o.get("id"), "fill": float(px) if px else None,
            "qty": int(float(o.get("filled_qty") or 0)), "at": o.get("filled_at")}  # fmt: skip


async def sync(engine: Engine, now: float | None = None) -> int:
    """Send entries for new playbook-autopilot calls, exits for closed ones, and read the fills.
    Returns orders sent."""
    dev = device(engine)
    o = engine.cfg.options
    if not dev or not (o.alpaca_key and o.alpaca_secret):
        return 0
    now = now or time.time()
    p = engine.desk.profiles.get(dev) or {}
    rules = _playbook_rules(p)
    lg = log(engine)
    sent = 0
    for t in p.get("tracked") or []:  # new calls: in with them
        if t["id"] in lg or t.get("auto") not in rules or now - float(t.get("ts") or 0) > FRESH_S:
            continue
        qty = max(1, int(t.get("shares") or 1))
        side = "buy" if t["direction"] == "long" else "sell"
        row = {"symbol": t["symbol"], "direction": t["direction"], "qty": qty, "alert_id": t.get("alert_id"),
               "desk_entry": t.get("entry"), "opened": now, "entry": {"status": "sending"}}  # fmt: skip
        try:
            row["entry"] = _leg(
                await _call(
                    engine, "POST", "/v2/orders", json=_order(t["symbol"], qty, side, f"ft-in-{t['id']}")
                )
            )
        except MirrorError as e:
            row["entry"] = {"status": "error", "why": str(e)}
        lg[t["id"]] = row
        sent += 1
    open_ids = {t["id"] for t in p.get("tracked") or []}
    closed = {c["id"]: c for c in p.get("closed") or []}
    for tid, row in lg.items():
        en, ex = row.get("entry") or {}, row.get("exit")
        if en.get("status") not in FINAL | {"error"} and en.get("order"):  # read the entry's fill
            try:
                en = row["entry"] = _leg(await _call(engine, "GET", f"/v2/orders/{en['order']}"))
            except MirrorError:
                pass
        if ex is None and tid not in open_ids and tid in closed:  # the desk closed it: out with it
            row["desk_exit"] = closed[tid].get("exit")
            row["desk_pnl"] = closed[tid].get("pnl_pct")
            if en.get("status") == "filled" and en.get("qty"):
                side = "sell" if row["direction"] == "long" else "buy"
                try:
                    row["exit"] = _leg(
                        await _call(
                            engine,
                            "POST",
                            "/v2/orders",
                            json=_order(row["symbol"], en["qty"], side, f"ft-out-{tid}"),
                        )
                    )
                except MirrorError as e:
                    row["exit"] = {"status": "error", "why": str(e)}
                sent += 1
            elif en.get("status") in FINAL | {"error"}:
                row["exit"] = {"status": "no position"}  # the entry never filled: nothing to close
        elif ex and ex.get("order") and ex.get("status") not in FINAL:
            try:
                row["exit"] = _leg(await _call(engine, "GET", f"/v2/orders/{ex['order']}"))
            except MirrorError:
                pass
    _save(engine, lg)
    return sent


def costs(row: dict[str, Any]) -> dict[str, Any] | None:
    """{"entry", "exit", "round", "desk", "broker"} for a mirrored call filled both ways, % (+ = cost)."""
    en, ex = row.get("entry") or {}, row.get("exit") or {}
    if not (en.get("fill") and ex.get("fill") and row.get("desk_entry") and row.get("desk_exit")):
        return None
    s = 1 if row["direction"] == "long" else -1
    entry = s * (en["fill"] / row["desk_entry"] - 1) * 100
    exit_ = -s * (ex["fill"] / row["desk_exit"] - 1) * 100
    broker = s * (ex["fill"] / en["fill"] - 1) * 100
    desk = row.get("desk_pnl")
    if desk is None:
        desk = s * (row["desk_exit"] / row["desk_entry"] - 1) * 100
    return {"entry": round(entry, 3), "exit": round(exit_, 3), "round": round(entry + exit_, 3), "desk": round(desk, 2),
            "broker": round(broker, 2)}  # fmt: skip


def _avg(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 3) if xs else None


def report(engine: Engine) -> dict[str, Any]:
    """The mirror's record: costs per call, by liquidity band, and in all."""
    from .liquidity import LABELS

    lg = log(engine)
    rows, bands = [], {}
    for tid, row in sorted(lg.items(), key=lambda kv: -kv[1].get("opened", 0)):
        c = costs(row)
        a = engine.storage.get_alert(row["alert_id"]) if row.get("alert_id") else None
        band = (((a or {}).get("edge") or {}).get("liq") or {}).get("band")
        rows.append({"id": tid, **{k: row.get(k) for k in ("symbol", "direction", "qty", "opened", "desk_entry", "desk_exit")},
                     "entry": row.get("entry"), "exit": row.get("exit"), "costs": c, "band": band})  # fmt: skip
        if c:
            bands.setdefault(band or "?", []).append(c)
    done = [r["costs"] for r in rows if r["costs"]]
    out: dict[str, Any] = {
        "on": bool(device(engine)),
        "keys": bool(engine.cfg.options.alpaca_key and engine.cfg.options.alpaca_secret),
        "n": len(done),
        "entry": _avg([c["entry"] for c in done]),
        "exit": _avg([c["exit"] for c in done]),
        "round": _avg([c["round"] for c in done]),
        "desk": _avg([c["desk"] for c in done]),
        "broker": _avg([c["broker"] for c in done]),
        "bands": [
            {
                "band": b,
                "label": LABELS.get(b, "Unknown size"),
                "n": len(cs),
                "round": _avg([c["round"] for c in cs]),
            }
            for b, cs in bands.items()
        ],  # fmt: skip
        "rows": rows[:30],
    }
    out["line"] = (
        f"On {out['n']} mirrored call{'s' if out['n'] != 1 else ''}, real paper fills cost {out['round']:+.2f}% a round "
        f"trip against the desk's prices ({out['entry']:+.2f}% in, {out['exit']:+.2f}% out): the broker made "
        f"{out['broker']:+.2f}% a call where the desk's record says {out['desk']:+.2f}%."
        if out["n"]
        else ""
    )
    return out
