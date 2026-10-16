"""Your desk: what each phone cares about, kept on the server so pushes can respect it.

Every phone (browser) that opens Foretape gets a random device id (localStorage) and sends it
with each request. Its profile holds:

* **watchlist**: up to 60 symbols. Alerts on them get a "Mine" filter on the tape, and a push even
  at MEDIUM (the level that is normally only shown in the app) when ``watch_push`` is on.
* **prefs**: which kinds of alerts may push to this phone (news, radar and ignition, options,
  insider buys, macro, confluence), the lowest severity this phone wants, quiet hours (ET) when
  only CRITICAL alerts get through, and whether it gets the end-of-day report card.
* **price alerts**: "above $X", "below $X", "moves ±N% on the day", or "falls N% from its high
  since I set this" (trailing), checked against the live price every 15 seconds and pushed to this
  phone only, once.
* **tracked calls**: a paper position taken from an alert or a ticker page, at the live price,
  with its P&L since, an optional stop and target (each pushed once when hit); closed when you say
  so. The system never trades.

Profiles live in SQLite (``profiles`` table) and, when ``STATE_DB`` is set, in the state database
too, so a fresh container on a free host keeps them.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
DEVICE_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
CATEGORIES = ("news", "radar", "options", "insider", "macro", "confluence")
SEV_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}
MAX_WATCH = 60
MAX_PRICE_ALERTS = 40
MAX_TRACKED = 40
MACRO_TICKERS = {
    "SPY",
    "QQQ",
    "IWM",
    "DIA",
    "TLT",
    "USO",
    "XLE",
    "GLD",
    "UUP",
    "FXE",
    "FXY",
    "FXB",
    "VIX",
    "^VIX",
}


def default_profile() -> dict[str, Any]:
    return {
        "watchlist": [],
        "prefs": {
            "cats": dict.fromkeys(CATEGORIES, True),
            "quiet": None,
            "watch_push": True,
            "min_sev": None,  # None: whatever the server sends; else this phone's own floor
            "eod": True,  # the end-of-day report card
        },
        "price_alerts": [],
        "tracked": [],
        "closed": [],
        "created": time.time(),
    }


def clean_symbol(raw: Any) -> str | None:
    s = str(raw or "").strip().upper().lstrip("$").replace("/", ".")
    return s if SYMBOL_RE.match(s) else None


def alert_category(alert: Any) -> str:
    """news | radar | options | insider | macro | system, for an Alert or its dict."""
    get = (
        (lambda k, d=None: alert.get(k, d))
        if isinstance(alert, dict)
        else (lambda k, d=None: getattr(alert, k, d))
    )
    kind = get("kind") or ""
    edge = get("edge") or {}
    item = get("item")
    extra = (item.get("extra") if isinstance(item, dict) else getattr(item, "extra", None)) or {}
    if kind == "system":
        return "system"
    if kind == "confluence":
        return "confluence"
    if extra.get("insider_buy"):
        return "insider"
    if kind == "options":
        return "options"
    if kind == "price" and (edge.get("radar") or edge.get("ignition") or edge.get("smallcap")):
        return "radar"
    tickers = [t for t in (get("tickers") or []) if t]
    if kind in ("news", "price") and (not tickers or set(tickers) <= MACRO_TICKERS):
        return "macro"
    return "news"


def in_quiet(quiet: dict[str, Any] | None, now: float | None = None) -> bool:
    """Quiet hours "22:00"-"07:00" ET (wrapping midnight allowed)."""
    if not quiet or not quiet.get("start") or not quiet.get("end"):
        return False
    try:
        h0, m0 = (int(x) for x in str(quiet["start"]).split(":"))
        h1, m1 = (int(x) for x in str(quiet["end"]).split(":"))
    except ValueError:
        return False
    t = datetime.fromtimestamp(now or time.time(), ET)
    cur, a, b = t.hour * 60 + t.minute, h0 * 60 + m0, h1 * 60 + m1
    return (a <= cur < b) if a <= b else (cur >= a or cur < b)


class Desk:
    def __init__(self, storage: Any, state: Any = None) -> None:
        self.storage = storage
        self.state = state  # StateStore (optional): survives a fresh container
        self.profiles: dict[str, dict[str, Any]] = {}
        self._dirty = False
        for device, data in storage.profiles_all().items():
            self.profiles[device] = self._merge(data)

    @staticmethod
    def _merge(data: dict[str, Any]) -> dict[str, Any]:
        """A stored profile over today's defaults (profiles saved before a pref existed get it)."""
        base = default_profile()
        prefs = {**base["prefs"], **(data.get("prefs") or {})}
        prefs["cats"] = {**base["prefs"]["cats"], **(prefs.get("cats") or {})}
        return {**base, **data, "prefs": prefs}

    async def restore(self) -> None:
        if self.state is None or self.profiles:
            return
        blob = await self.state.get("desk-profiles")
        for device, data in json.loads(blob or b"{}").items():
            if DEVICE_RE.match(device):
                self.profiles[device] = self._merge(data)
                self.storage.profile_save(device, self.profiles[device])

    async def persist(self) -> None:
        if self.state is not None and self._dirty:
            await self.state.put("desk-profiles", json.dumps(self.profiles).encode())
        self._dirty = False

    # ------------------------------------------------------------------ profiles

    @staticmethod
    def valid_device(device: str | None) -> bool:
        return bool(device and DEVICE_RE.match(device))

    def get(self, device: str) -> dict[str, Any]:
        p = self.profiles.get(device)
        if p is None:
            p = self.profiles[device] = default_profile()
        return p

    def save(self, device: str) -> None:
        self.storage.profile_save(device, self.profiles[device])
        self._dirty = True

    def watchers(self, symbols: list[str]) -> list[str]:
        """Devices whose watchlist has any of these symbols."""
        want = {s.upper() for s in symbols}
        return [d for d, p in self.profiles.items() if want & set(p.get("watchlist") or [])]

    def allows(self, device: str, alert: Any, now: float | None = None) -> bool:
        """May this alert push to this phone? Its category must be on, and in quiet hours only
        CRITICAL gets through. A phone with no profile gets everything (as before)."""
        p = self.profiles.get(device)
        if p is None:
            return True
        prefs = p.get("prefs") or {}
        cat = alert_category(alert)
        if cat != "system" and not (prefs.get("cats") or {}).get(cat, True):
            return False
        sev = (
            alert.get("severity")
            if isinstance(alert, dict)
            else getattr(getattr(alert, "severity", None), "name", "")
        )
        floor = prefs.get("min_sev")
        if cat != "system" and floor in SEV_RANK and SEV_RANK.get(str(sev), 0) < SEV_RANK[floor]:
            return False
        return not (in_quiet(prefs.get("quiet"), now) and str(sev) != "CRITICAL")

    # ------------------------------------------------------------------ edits

    def apply(self, device: str, action: str, body: dict[str, Any], price_of: Any = None) -> dict[str, Any]:
        """One edit from the app. Returns the updated profile. Raises ValueError on bad input."""
        p = self.get(device)
        now = time.time()
        if action == "watch_add":
            syms = [s for s in (clean_symbol(x) for x in body.get("symbols") or [body.get("symbol")]) if s]
            if not syms:
                raise ValueError("no valid symbol")
            wl = list(dict.fromkeys([*(p["watchlist"]), *syms]))
            p["watchlist"] = wl[:MAX_WATCH]
        elif action == "watch_remove":
            sym = clean_symbol(body.get("symbol"))
            p["watchlist"] = [s for s in p["watchlist"] if s != sym]
        elif action == "prefs":
            prefs = p["prefs"]
            for cat, on in (body.get("cats") or {}).items():
                if cat in CATEGORIES:
                    prefs["cats"][cat] = bool(on)
            if "quiet" in body:
                q = body.get("quiet")
                if (
                    q
                    and re.match(r"^\d{1,2}:\d{2}$", str(q.get("start", "")))
                    and re.match(r"^\d{1,2}:\d{2}$", str(q.get("end", "")))
                ):
                    prefs["quiet"] = {"start": q["start"], "end": q["end"]}
                else:
                    prefs["quiet"] = None
            if "watch_push" in body:
                prefs["watch_push"] = bool(body["watch_push"])
            if "min_sev" in body:
                floor = body.get("min_sev")
                prefs["min_sev"] = floor if floor in SEV_RANK else None
            if "eod" in body:
                prefs["eod"] = bool(body["eod"])
        elif action == "alert_add":
            sym = clean_symbol(body.get("symbol"))
            kind = str(body.get("kind") or "")
            try:
                value = float(body.get("value"))
            except (TypeError, ValueError):
                raise ValueError("value must be a number") from None
            if not sym or kind not in ("above", "below", "move", "trail") or value <= 0:
                raise ValueError("need a symbol, kind above|below|move|trail and a positive value")
            if kind == "trail" and value >= 100:
                raise ValueError("a trailing alert is a % drop from the high: under 100")
            if len([a for a in p["price_alerts"] if not a.get("fired")]) >= MAX_PRICE_ALERTS:
                raise ValueError(f"at most {MAX_PRICE_ALERTS} price alerts")
            p["price_alerts"].append(
                {
                    "id": secrets.token_hex(5),
                    "symbol": sym,
                    "kind": kind,
                    "value": value,
                    "created": now,
                    "fired": None,
                }
                | ({"peak": None} if kind == "trail" else {})
            )
        elif action == "alert_remove":
            aid = str(body.get("id") or "")
            p["price_alerts"] = [a for a in p["price_alerts"] if a["id"] != aid]
        elif action == "track_add":
            sym = clean_symbol(body.get("symbol"))
            direction = str(body.get("direction") or "long")
            if not sym or direction not in ("long", "short"):
                raise ValueError("need a symbol and direction long|short")
            q = price_of(sym) if price_of else None
            entry = body.get("entry") or (q[1] if q else None)
            try:
                entry = float(entry)
            except (TypeError, ValueError):
                raise ValueError(f"no live price for {sym} yet: enter one") from None
            if len(p["tracked"]) >= MAX_TRACKED:
                raise ValueError(f"at most {MAX_TRACKED} tracked calls")
            t = {
                "id": secrets.token_hex(5),
                "symbol": sym,
                "direction": direction,
                "entry": round(entry, 4),
                "ts": now,
                "alert_id": str(body.get("alert_id") or "")[:40],
                "note": str(body.get("note") or "")[:140],
                "stop": None,
                "target": None,
            }
            _set_levels(t, body)
            p["tracked"].append(t)
        elif action == "track_levels":
            tid = str(body.get("id") or "")
            t = next((x for x in p["tracked"] if x["id"] == tid), None)
            if t is None:
                raise ValueError("no such tracked call")
            _set_levels(t, body)
        elif action == "track_close":
            tid = str(body.get("id") or "")
            keep = []
            for t in p["tracked"]:
                if t["id"] != tid:
                    keep.append(t)
                    continue
                q = price_of(t["symbol"]) if price_of else None
                exit_ = q[1] if q else None
                pnl = _pnl(t, exit_)
                p["closed"] = ([{**t, "exit": exit_, "closed": now, "pnl_pct": pnl}] + p["closed"])[:50]
            p["tracked"] = keep
        else:
            raise ValueError(f"unknown action {action!r}")
        self.save(device)
        return p

    # ------------------------------------------------------------------ live views

    def view(self, device: str, price_of: Any) -> dict[str, Any]:
        """The profile with live prices: watchlist moves, price alerts' distance, tracked P&L."""
        p = self.get(device)
        watch = []
        for sym in p["watchlist"]:
            q = price_of(sym)
            watch.append({"symbol": sym, "price": q[1] if q else None, "chg_day": q[2] if q else None})
        alerts = []
        for a in p["price_alerts"]:
            q = price_of(a["symbol"])
            alerts.append({**a, "price": q[1] if q else None, "chg_day": q[2] if q else None})
        tracked = []
        for t in p["tracked"]:
            q = price_of(t["symbol"])
            tracked.append({**t, "price": q[1] if q else None, "pnl_pct": _pnl(t, q[1] if q else None)})
        closed = p.get("closed") or []
        wins = [c for c in closed if (c.get("pnl_pct") or 0) > 0]
        return {
            "device": device,
            "watchlist": watch,
            "prefs": p["prefs"],
            "price_alerts": alerts,
            "tracked": tracked,
            "closed": closed[:20],
            "record": {
                "closed": len(closed),
                "wins": len(wins),
                "avg_pnl_pct": round(sum(c.get("pnl_pct") or 0 for c in closed) / len(closed), 2)
                if closed
                else None,
            },
        }

    def due_price_alerts(
        self, price_of: Any, now: float | None = None
    ) -> list[tuple[str, dict[str, Any], tuple]]:
        """(device, alert, quote) for every price alert whose condition holds now; marks them fired."""
        now = now or time.time()
        out = []
        for device, p in self.profiles.items():
            changed = False
            for a in p.get("price_alerts") or []:
                if a.get("fired"):
                    continue
                q = price_of(a["symbol"])
                if not q or q[1] is None:
                    continue
                price, chg = q[1], q[2]
                if a["kind"] == "trail" and (a.get("peak") is None or price > a["peak"]):
                    a["peak"] = price  # the high since it was set: the first quote starts it
                    changed = True
                    continue
                hit = (
                    (a["kind"] == "above" and price >= a["value"])
                    or (a["kind"] == "below" and price <= a["value"])
                    or (a["kind"] == "move" and chg is not None and abs(chg) >= a["value"])
                    or (a["kind"] == "trail" and price <= a["peak"] * (1 - a["value"] / 100.0))
                )
                if hit:
                    a["fired"] = now
                    a["fired_price"] = price
                    changed = True
                    out.append((device, a, q))
            if changed:
                self.save(device)
        return out

    def due_levels(
        self, price_of: Any, now: float | None = None
    ) -> list[tuple[str, dict[str, Any], str, tuple]]:
        """(device, tracked call, "stop" | "target", quote) for every stop or target the live price
        reached; each fires once."""
        now = now or time.time()
        out = []
        for device, p in self.profiles.items():
            changed = False
            for t in p.get("tracked") or []:
                if t.get("stop") is None and t.get("target") is None:
                    continue
                q = price_of(t["symbol"])
                if not q or q[1] is None:
                    continue
                for which in ("stop", "target"):
                    lvl = t.get(which)
                    if lvl is None or t.get(f"{which}_hit"):
                        continue
                    if (q[1] <= lvl) if _under(t, which) else (q[1] >= lvl):
                        t[f"{which}_hit"] = now
                        t[f"{which}_price"] = q[1]
                        changed = True
                        out.append((device, t, which, q))
            if changed:
                self.save(device)
        return out


def _under(t: dict[str, Any], which: str) -> bool:
    """A long's stop and a short's target sit under the entry."""
    return (which == "stop") == (t.get("direction", "long") == "long")


def _set_levels(t: dict[str, Any], body: dict[str, Any]) -> None:
    """A stop and a target on a tracked call, each on its side of the entry."""
    for which in ("stop", "target"):
        if which not in body:
            continue
        raw = body.get(which)
        if raw in (None, ""):
            t[which] = None
            t.pop(f"{which}_hit", None)
            continue
        try:
            v = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{which} must be a price") from None
        under = _under(t, which)
        if v <= 0 or (under and v >= t["entry"]) or (not under and v <= t["entry"]):
            side = "under" if under else "over"
            raise ValueError(f"a {t['direction']}'s {which} goes {side} its entry (${t['entry']:,.2f})")
        t[which] = round(v, 4)
        t.pop(f"{which}_hit", None)


def _pnl(t: dict[str, Any], price: float | None) -> float | None:
    if price is None or not t.get("entry"):
        return None
    r = (price / t["entry"] - 1.0) * 100.0
    return round(r if t.get("direction", "long") == "long" else -r, 2)
