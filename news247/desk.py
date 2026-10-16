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
  with its P&L since, an optional stop and target (each pushed once when hit), and a journal (a
  note and #tags, so the record can be read by tag); closed when you say so. The system never
  trades.
* **keywords**: up to 20 words or phrases; any scored headline that names one is pushed to this
  phone alone, even below the alert line (the last 30 hits are kept).
* **muted**: stocks silenced on this phone for a while (an alert only about muted stocks stays in
  the app).
* **digest**: instead of one push per alert, this phone can get one push every 15 or 60 minutes
  listing what came in (CRITICAL still comes at once; price alerts and levels too).
* **notes** on any stock, and a **group** for each watched stock.
* **transfer**: a one-time code (15 minutes) that copies this desk to another phone.
* **rules** (smart alerts): this phone's own triggers on a stock or on every watched stock: a
  headline scoring N+, a headline followed by an N% move within the hour, N times normal volume,
  a VWAP cross, an N% pre-market gap. Each fires once per headline (or once a day).
* **share**: a code (7 days, any number of uses) that copies this watchlist into someone else's.
* **holdings**: shares (and an optional cost) per stock: the portfolio view; held stocks count as
  watched for pushes.
* **follow**: themes (the scorer's: AI launches, export controls, FDA actions...) and sectors;
  MEDIUM alerts on them reach this phone like a watched stock's.

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
MAX_KEYWORDS = 20
TRANSFER_TTL_S = 900.0
TRANSFER_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I/L: read aloud or typed by hand
DIGEST_MINUTES = (0, 15, 60)
MAX_RULES = 30
SHARE_TTL_S = 7 * 86400.0
RULE_KINDS = {  # kind -> (lowest value, highest value, default)
    "headline": (5.0, 100.0, 20.0),  # a headline on the stock scoring at least this
    "news_move": (0.5, 50.0, 3.0),  # a headline, then the stock moves this % (either way) within the hour
    "volume": (1.5, 50.0, 5.0),  # this many times its normal volume for the time of day
    "vwap": (0.0, 0.0, 0.0),  # crosses VWAP (``dir``: above | below)
    "gap": (1.0, 50.0, 4.0),  # this % from yesterday's close before the open
}
MAX_MUTE_H = 72.0
TAG_RE = re.compile(r"#([a-z][a-z0-9-]{1,19})")
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
            "digest": 0,  # minutes: 0 = push each alert; 15 or 60 = one push listing them
            "earn_remind": True,  # a push before a watched stock reports
            "open_plan": True,  # the 9:20 ET open plan
            "week": True,  # Friday's "your week"
            "skip_fades": False,  # don't push alert groups whose base rate says they fade
            "auto_move": 0,  # %: a "moves ±N% on the day" price alert for every stock added (0 = off)
            "forward": None,  # this phone's own copy: {"kind": "discord"|"telegram"|"ntfy", ...}
            "close_recap": False,  # the 16:15 ET market recap ("the close")
            "tomorrow": False,  # the 20:00 ET "tomorrow" card
            "followups": False,  # an hour after an alert on your stock: how it played out (2%+ moves)
        },
        "price_alerts": [],
        "tracked": [],
        "closed": [],
        "keywords": [],  # [{"k": "tariff", "since": ts}]
        "kw_hits": [],  # newest first: {"k", "title", "url", "source", "ts", "score"}
        "muted": {},  # symbol -> until (epoch)
        "notes": {},  # symbol -> {"text", "ts"}
        "groups": {},  # watched symbol -> group name
        "votes": {},  # alert id -> +1 / -1 (was it useful)
        "snooze": {},  # category -> until (epoch)
        "saved": [],  # alert ids, newest first
        "rules": [],  # smart alerts: {"id", "kind", "symbol" ("*" = every watched stock), "value", "dir"}
        "rules_fired": [],  # keys of what each rule already fired on (once per headline / per day)
        "holdings": {},  # symbol -> {"shares", "cost"}
        "taste": {},  # record key -> {"up", "down", "saved", "tracked", "opened"}: what this phone finds useful
        "opened": [],  # alert ids recently opened (counted once)
        "follow": {"themes": [], "sectors": []},
        "digest_q": [],  # alerts held for the next digest
        "digest_at": 0.0,  # when the last digest went
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
        return [
            d
            for d, p in self.profiles.items()
            if want & (set(p.get("watchlist") or []) | set(p.get("holdings") or {}))
        ]

    def followers(self, themes: list[str], sectors: list[str]) -> list[str]:
        """Devices following any of these themes or sectors."""
        th, se = set(themes), set(sectors)
        out = []
        for d, p in self.profiles.items():
            f = p.get("follow") or {}
            if th & set(f.get("themes") or []) or se & set(f.get("sectors") or []):
                out.append(d)
        return out

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
        sev_name = (
            alert.get("severity")
            if isinstance(alert, dict)
            else getattr(getattr(alert, "severity", None), "name", "")
        )
        if str(sev_name) != "CRITICAL":
            if (p.get("snooze") or {}).get(cat, 0) > (now or time.time()):
                return False
            edge = (alert.get("edge") if isinstance(alert, dict) else getattr(alert, "edge", None)) or {}
            if prefs.get("skip_fades") and (edge.get("base") or {}).get("fades"):
                return False
        sev = sev_name
        if self.all_muted(device, alert, now):
            return False
        floor = prefs.get("min_sev")
        if cat != "system" and floor in SEV_RANK and SEV_RANK.get(str(sev), 0) < SEV_RANK[floor]:
            return False
        return not (in_quiet(prefs.get("quiet"), now) and str(sev) != "CRITICAL")

    def all_muted(self, device: str, alert: Any, now: float | None = None) -> bool:
        """Is every stock this alert is about muted on this phone? (No stocks: never muted.)"""
        muted = (self.profiles.get(device) or {}).get("muted") or {}
        if not muted:
            return False
        tickers = alert.get("tickers") if isinstance(alert, dict) else getattr(alert, "tickers", None)
        now = now or time.time()
        tickers = [t for t in (tickers or []) if t]
        return bool(tickers) and all(muted.get(t, 0) > now for t in tickers)

    def hold(self, device: str, alert: Any, now: float | None = None) -> bool:
        """In digest mode, keep a non-CRITICAL alert for this phone's next digest instead of pushing
        it now. Returns True when held."""
        p = self.profiles.get(device)
        if not p or not (p.get("prefs") or {}).get("digest"):
            return False
        get = (lambda k: alert.get(k)) if isinstance(alert, dict) else (lambda k: getattr(alert, k, None))
        sev = get("severity")
        sev = sev if isinstance(sev, str) else getattr(sev, "name", "")
        if sev == "CRITICAL":
            return False
        q = p.setdefault("digest_q", [])
        aid = str(get("id") or "")
        if aid and any(x["id"] == aid for x in q):
            return True
        q.append({"id": aid, "title": str(get("title") or "")[:160], "severity": sev, "tickers": list(get("tickers") or [])[:4],
                  "ts": now or time.time()})  # fmt: skip
        del q[:-50]
        self._dirty = True
        return True

    def due_digests(self, now: float | None = None) -> list[tuple[str, list[dict[str, Any]]]]:
        """(device, held alerts) for every phone whose digest is due; empties their queues."""
        now = now or time.time()
        out = []
        for device, p in self.profiles.items():
            mins = (p.get("prefs") or {}).get("digest") or 0
            q = p.get("digest_q") or []
            if not q:
                continue
            if mins and now - (p.get("digest_at") or 0) < mins * 60:
                continue
            out.append((device, list(q)))
            p["digest_q"], p["digest_at"] = [], now
            self.save(device)
        return out

    def new_transfer(self, device: str, now: float | None = None) -> str:
        now = now or time.time()
        self.transfers = {c: v for c, v in getattr(self, "transfers", {}).items() if v[1] > now}
        code = "".join(secrets.choice(TRANSFER_ALPHABET) for _ in range(8))
        self.transfers[code] = (device, now + TRANSFER_TTL_S)
        return code

    def keyword_devices(self) -> dict[str, list[tuple[str, re.Pattern[str]]]]:
        """device -> [(keyword, compiled whole-word pattern)] for every phone with keywords."""
        out = {}
        for d, p in self.profiles.items():
            kws = p.get("keywords") or []
            if kws:
                out[d] = [(k["k"], _kw_pattern(k["k"])) for k in kws]
        return out

    def record_kw_hit(self, device: str, hit: dict[str, Any]) -> None:
        p = self.get(device)
        p["kw_hits"] = ([hit] + (p.get("kw_hits") or []))[:30]
        self.save(device)

    # ------------------------------------------------------------------ edits

    def apply(self, device: str, action: str, body: dict[str, Any], price_of: Any = None) -> dict[str, Any]:
        """One edit from the app. Returns the updated profile. Raises ValueError on bad input."""
        p = self.get(device)
        now = time.time()
        if action == "watch_add":
            syms = [s for s in (clean_symbol(x) for x in body.get("symbols") or [body.get("symbol")]) if s]
            if not syms:
                raise ValueError("no valid symbol")
            fresh = [x for x in syms if x not in p["watchlist"]]
            wl = list(dict.fromkeys([*(p["watchlist"]), *syms]))
            p["watchlist"] = wl[:MAX_WATCH]
            auto = (p.get("prefs") or {}).get("auto_move") or 0
            if auto:
                open_ = [a for a in p["price_alerts"] if not a.get("fired")]
                for x in fresh:
                    if (
                        x in p["watchlist"]
                        and len(open_) < MAX_PRICE_ALERTS
                        and not any(a["symbol"] == x and a["kind"] == "move" for a in open_)
                    ):
                        a = {
                            "id": secrets.token_hex(5),
                            "symbol": x,
                            "kind": "move",
                            "value": float(auto),
                            "created": now,
                            "fired": None,
                            "auto": True,
                        }
                        p["price_alerts"].append(a)
                        open_.append(a)
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
            for flag in (
                "eod",
                "earn_remind",
                "open_plan",
                "week",
                "skip_fades",
                "close_recap",
                "tomorrow",
                "followups",
            ):
                if flag in body:
                    prefs[flag] = bool(body[flag])
            if "auto_move" in body:
                try:
                    v = float(body.get("auto_move") or 0)
                except (TypeError, ValueError):
                    v = -1
                if v not in (0, 3, 5, 8, 10):
                    raise ValueError("auto_move is 0 (off), 3, 5, 8 or 10")
                prefs["auto_move"] = v
            if "forward" in body:
                prefs["forward"] = clean_forward(body.get("forward"))
            if "digest" in body:
                try:
                    mins = int(body.get("digest") or 0)
                except (TypeError, ValueError):
                    mins = -1
                if mins not in DIGEST_MINUTES:
                    raise ValueError("digest is 0, 15 or 60 minutes")
                prefs["digest"] = mins
                if not mins:
                    p["digest_at"] = 0.0  # what's held goes out on the next pass
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
                    "rearm": bool(body.get("rearm")),
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
            _journal(t, body)
            p["tracked"].append(t)
            if t["alert_id"]:
                self._taste(p, t["alert_id"], "tracked", 1)
        elif action == "track_levels":
            tid = str(body.get("id") or "")
            t = next((x for x in p["tracked"] if x["id"] == tid), None)
            if t is None:
                raise ValueError("no such tracked call")
            _set_levels(t, body)
        elif action == "track_journal":
            tid = str(body.get("id") or "")
            t = next((x for x in [*p["tracked"], *p["closed"]] if x["id"] == tid), None)
            if t is None:
                raise ValueError("no such call")
            _journal(t, body)
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
                done = {**t, "exit": exit_, "closed": now, "pnl_pct": pnl}
                if body.get("lesson"):
                    done["lesson"] = str(body["lesson"])[:200]
                p["closed"] = ([done] + p["closed"])[:50]
            p["tracked"] = keep
        elif action == "note":
            sym = clean_symbol(body.get("symbol"))
            if not sym:
                raise ValueError("no valid symbol")
            text = str(body.get("text") or "").strip()[:500]
            notes = p.setdefault("notes", {})
            if text:
                notes[sym] = {"text": text, "ts": now}
            else:
                notes.pop(sym, None)
        elif action == "watch_group":
            sym = clean_symbol(body.get("symbol"))
            if not sym or sym not in p["watchlist"]:
                raise ValueError("not on your watchlist")
            g = re.sub(r"\s+", " ", str(body.get("group") or "")).strip()[:24]
            groups = p.setdefault("groups", {})
            if g:
                groups[sym] = g
            else:
                groups.pop(sym, None)
        elif action == "vote":
            aid = str(body.get("id") or "")[:40]
            v = int(body.get("v") or 0)
            if not aid or v not in (-1, 0, 1):
                raise ValueError("need an alert id and v of 1, -1 or 0")
            votes = p.setdefault("votes", {})
            before = votes.get(aid, 0)
            if v:
                votes[aid] = v
            else:
                votes.pop(aid, None)
            if len(votes) > 400:
                for k in list(votes)[:100]:
                    del votes[k]
            self._tally_vote(aid, before, v)
            if before != v:
                if before:
                    self._taste(p, aid, "up" if before > 0 else "down", -1)
                if v:
                    self._taste(p, aid, "up" if v > 0 else "down", 1)
        elif action == "transfer_out":
            p["transfer"] = {"code": self.new_transfer(device, now), "until": now + TRANSFER_TTL_S}
        elif action == "transfer_in":
            code = str(body.get("code") or "").strip().upper().replace(" ", "").replace("-", "")
            hit = getattr(self, "transfers", {}).get(code)
            if not hit or hit[1] < now:
                raise ValueError("that code is wrong or expired: make a new one on the other phone")
            src, _ = hit
            del self.transfers[code]
            if src == device:
                raise ValueError("that code is from this phone")
            copied = json.loads(json.dumps(self.profiles[src]))
            copied["digest_q"], copied["created"] = [], p.get("created", now)
            self.profiles[device] = p = self._merge(copied)
        elif action == "rule_add":
            kind = str(body.get("kind") or "")
            if kind not in RULE_KINDS:
                raise ValueError("rule kind is headline, news_move, volume, vwap or gap")
            raw = str(body.get("symbol") or "").strip()
            sym = "*" if raw == "*" else clean_symbol(raw)
            if not sym:
                raise ValueError("need a symbol, or * for every stock you watch")
            lo, hi, default = RULE_KINDS[kind]
            if kind == "vwap":
                d = str(body.get("dir") or "")
                if d not in ("above", "below"):
                    raise ValueError("a VWAP rule needs dir above or below")
                value = 0.0
            else:
                d = ""
                try:
                    value = float(body.get("value") if body.get("value") not in (None, "") else default)
                except (TypeError, ValueError):
                    raise ValueError("value must be a number") from None
                if not lo <= value <= hi:
                    raise ValueError(f"{kind} value is {lo:g} to {hi:g}")
            rules = p.setdefault("rules", [])
            if len(rules) >= MAX_RULES:
                raise ValueError(f"at most {MAX_RULES} rules")
            rules.append(
                {
                    "id": secrets.token_hex(4),
                    "kind": kind,
                    "symbol": sym,
                    "value": value,
                    "dir": d,
                    "created": now,
                }
            )
        elif action == "rule_remove":
            rid = str(body.get("id") or "")
            p["rules"] = [r for r in p.get("rules") or [] if r["id"] != rid]
        elif action == "share_list":
            if not p["watchlist"]:
                raise ValueError("your watchlist is empty")
            code = "".join(secrets.choice(TRANSFER_ALPHABET) for _ in range(8))
            name = str(body.get("name") or "").strip()[:40]
            if hasattr(self.storage, "set_setting"):
                self.storage.set_setting(
                    f"share:{code}",
                    json.dumps({"symbols": p["watchlist"], "name": name, "until": now + SHARE_TTL_S}),
                )
            p["share"] = {"code": code, "until": now + SHARE_TTL_S, "n": len(p["watchlist"]), "name": name}
        elif action == "import_list":
            code = str(body.get("code") or "").strip().upper().replace(" ", "").replace("-", "")
            raw = self.storage.get_setting(f"share:{code}") if hasattr(self.storage, "get_setting") else None
            data = json.loads(raw) if raw else None
            if not data or data.get("until", 0) < now:
                raise ValueError("that list code is wrong or expired")
            syms = [x for x in (clean_symbol(s) for s in data.get("symbols") or []) if x]
            p["watchlist"] = list(dict.fromkeys([*p["watchlist"], *syms]))[:MAX_WATCH]
            if data.get("name"):
                for x in syms:
                    p.setdefault("groups", {}).setdefault(x, data["name"][:24])
        elif action == "hold_set":
            sym = clean_symbol(body.get("symbol"))
            try:
                shares = float(body.get("shares") or 0)
                cost = float(body["cost"]) if body.get("cost") not in (None, "") else None
            except (TypeError, ValueError):
                raise ValueError("shares and cost are numbers") from None
            if not sym or shares < 0 or (cost is not None and cost <= 0):
                raise ValueError(
                    "need a symbol, shares (0 removes it) and an optional positive cost per share"
                )
            h = p.setdefault("holdings", {})
            if shares == 0:
                h.pop(sym, None)
            else:
                if len(h) >= 100 and sym not in h:
                    raise ValueError("at most 100 holdings")
                h[sym] = {"shares": shares, "cost": cost}
        elif action == "follow":
            kind = str(body.get("kind") or "")
            name = str(body.get("name") or "").strip()[:60]
            if kind not in ("themes", "sectors") or not name:
                raise ValueError("follow needs kind themes|sectors and a name")
            f = p.setdefault("follow", {"themes": [], "sectors": []})
            cur = f.setdefault(kind, [])
            if body.get("on", True):
                if name not in cur and len(cur) < 30:
                    cur.append(name)
            else:
                f[kind] = [x for x in cur if x != name]
        elif action == "snooze":
            cat = str(body.get("cat") or "")
            try:
                hours = float(body.get("hours") or 0)
            except (TypeError, ValueError):
                hours = -1
            if cat not in CATEGORIES or not 0 <= hours <= 24:
                raise ValueError("need a category and 0-24 hours (0 ends the snooze)")
            sn = {c: u for c, u in (p.get("snooze") or {}).items() if u > now and c != cat}
            if hours:
                sn[cat] = now + hours * 3600
            p["snooze"] = sn
        elif action == "opened":
            aid = str(body.get("id") or "")[:40]
            seen = p.setdefault("opened", [])
            if aid and aid not in seen:
                seen.append(aid)
                del seen[:-200]
                self._taste(p, aid, "opened", 1)
        elif action in ("save", "unsave"):
            aid = str(body.get("id") or "")[:40]
            if not aid:
                raise ValueError("need an alert id")
            if action == "save" and aid not in (p.get("saved") or []):
                self._taste(p, aid, "saved", 1)
            saved = [x for x in p.get("saved") or [] if x != aid]
            p["saved"] = ([aid] + saved)[:100] if action == "save" else saved
        elif action == "kw_add":
            k = re.sub(r"\s+", " ", str(body.get("keyword") or "")).strip().lower()
            if not 2 <= len(k) <= 40:
                raise ValueError("a keyword is 2 to 40 characters")
            kws = p.setdefault("keywords", [])
            if all(x["k"] != k for x in kws):
                if len(kws) >= MAX_KEYWORDS:
                    raise ValueError(f"at most {MAX_KEYWORDS} keywords")
                kws.append({"k": k, "since": now})
        elif action == "kw_remove":
            k = str(body.get("keyword") or "").strip().lower()
            p["keywords"] = [x for x in p.get("keywords") or [] if x["k"] != k]
        elif action == "mute":
            sym = clean_symbol(body.get("symbol"))
            try:
                hours = float(body.get("hours") or 4)
            except (TypeError, ValueError):
                raise ValueError("hours must be a number") from None
            if not sym or not 0 < hours <= MAX_MUTE_H:
                raise ValueError(f"need a symbol and 0-{MAX_MUTE_H:g} hours")
            muted = {s: u for s, u in (p.get("muted") or {}).items() if u > now}  # forget expired ones
            muted[sym] = now + hours * 3600
            p["muted"] = muted
        elif action == "unmute":
            sym = clean_symbol(body.get("symbol"))
            p["muted"] = {s: u for s, u in (p.get("muted") or {}).items() if s != sym and u > now}
        else:
            raise ValueError(f"unknown action {action!r}")
        self.save(device)
        return p

    def _taste(self, p: dict[str, Any], aid: str, field: str, delta: int) -> None:
        """Count what this phone does with an alert (vote, save, track, open) under the alert's kind."""
        if not hasattr(self.storage, "get_alert"):
            return
        a = self.storage.get_alert(aid)
        if a is None:
            return
        from .dossier import record_key

        row = p.setdefault("taste", {}).setdefault(
            record_key(a), {"up": 0, "down": 0, "saved": 0, "tracked": 0, "opened": 0}
        )
        row[field] = max(0, row.get(field, 0) + delta)

    def _tally_vote(self, aid: str, before: int, after: int) -> None:
        """Keep the alert's own count of 👍/👎 (all phones) in its edge, for the record."""
        if before == after or not hasattr(self.storage, "get_alert"):
            return
        a = self.storage.get_alert(aid)
        if a is None:
            return
        fb = dict((a.get("edge") or {}).get("feedback") or {"up": 0, "down": 0})
        for v, d in ((before, -1), (after, 1)):
            if v:
                k = "up" if v > 0 else "down"
                fb[k] = max(0, fb.get(k, 0) + d)
        self.storage.update_alert_edge(aid, {"feedback": fb})

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
        by_tag: dict[str, list[float]] = {}
        for c in closed:
            for tag in c.get("tags") or ["untagged"]:
                by_tag.setdefault(tag, []).append(c.get("pnl_pct") or 0.0)
        now = time.time()
        return {
            "device": device,
            "watchlist": watch,
            "prefs": p["prefs"],
            "price_alerts": alerts,
            "tracked": tracked,
            "closed": closed[:20],
            "keywords": p.get("keywords") or [],
            "kw_hits": (p.get("kw_hits") or [])[:15],
            "muted": {s: u for s, u in (p.get("muted") or {}).items() if u > now},
            "notes": p.get("notes") or {},
            "groups": p.get("groups") or {},
            "votes": p.get("votes") or {},
            "held": len(p.get("digest_q") or []),
            "snooze": {c: u for c, u in (p.get("snooze") or {}).items() if u > now},
            "saved": p.get("saved") or [],
            "transfer": p["transfer"] if (p.get("transfer") or {}).get("until", 0) > now else None,
            "forward": _mask_forward((p.get("prefs") or {}).get("forward")),
            "rules": p.get("rules") or [],
            "holdings": p.get("holdings") or {},
            "taste": taste_rows(p.get("taste") or {}),
            "follow": p.get("follow") or {"themes": [], "sectors": []},
            "share": p["share"] if (p.get("share") or {}).get("until", 0) > now else None,
            "record": {
                "closed": len(closed),
                "wins": len(wins),
                "avg_pnl_pct": round(sum(c.get("pnl_pct") or 0 for c in closed) / len(closed), 2)
                if closed
                else None,
                "by_tag": sorted(
                    (
                        {
                            "tag": t,
                            "n": len(xs),
                            "wins": sum(1 for x in xs if x > 0),
                            "avg": round(sum(xs) / len(xs), 2),
                        }
                        for t, xs in by_tag.items()
                    ),
                    key=lambda r: -r["n"],
                ),
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
                if (
                    a.get("fired")
                    and a.get("rearm")
                    and datetime.fromtimestamp(a["fired"], ET).date()
                    != datetime.fromtimestamp(now, ET).date()
                ):
                    a["fired"], a["fired_price"] = None, None  # every day: a new day, armed again
                    if a["kind"] == "trail":
                        a["peak"] = None
                    changed = True
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


DISCORD_RE = re.compile(
    r"^https://(?:discord\.com|discordapp\.com|ptb\.discord\.com|canary\.discord\.com)/api/webhooks/\d{5,25}/[\w-]{20,120}$"
)
TELEGRAM_TOKEN_RE = re.compile(r"^\d{5,12}:[\w-]{30,60}$")
TELEGRAM_CHAT_RE = re.compile(r"^-?\d{3,20}$|^@[A-Za-z][\w]{4,31}$")
NTFY_TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def taste_rows(taste: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
    """What this phone finds useful, per alert kind: a score from -1 (votes it down) to 1 (votes it
    up, saves it, tracks it), shrunk toward 0 until there's enough to go on."""
    from .dossier import RECORD_LABEL

    out = []
    for key, t in taste.items():
        good = (
            2 * t.get("up", 0) + 1.5 * t.get("saved", 0) + 2 * t.get("tracked", 0) + 0.3 * t.get("opened", 0)
        )
        bad = 2 * t.get("down", 0)
        weight = (
            t.get("up", 0)
            + t.get("down", 0)
            + t.get("saved", 0)
            + t.get("tracked", 0)
            + 0.3 * t.get("opened", 0)
        )
        if not weight:
            continue
        cat, _, sev = key.partition(":")
        out.append({"key": key, "label": RECORD_LABEL.get(cat, cat.title()) + (f" · {sev}" if sev else ""), **t,
                    "score": round((good - bad) / (good + bad + 3), 2),
                    "suggest_off": t.get("down", 0) >= 3 and t.get("down", 0) > 2 * (t.get("up", 0) + t.get("saved", 0) + t.get("tracked", 0)),
                    "category": cat})  # fmt: skip
    out.sort(key=lambda r: -r["score"])
    return out


def clean_forward(raw: Any) -> dict[str, str] | None:
    """A phone's forward, checked: a Discord webhook URL on Discord's own hosts, a Telegram bot
    token and chat, or an ntfy.sh topic. Anything else is refused (the server only ever posts to
    those three services)."""
    if not raw:
        return None
    if not isinstance(raw, dict):
        raise ValueError("forward must be an object")
    kind = str(raw.get("kind") or "")
    if kind == "discord":
        url = str(raw.get("url") or "").strip()
        if not DISCORD_RE.match(url):
            raise ValueError("that isn't a Discord webhook URL (https://discord.com/api/webhooks/...)")
        return {"kind": "discord", "url": url}
    if kind == "telegram":
        token, chat = str(raw.get("token") or "").strip(), str(raw.get("chat") or "").strip()
        if not TELEGRAM_TOKEN_RE.match(token) or not TELEGRAM_CHAT_RE.match(chat):
            raise ValueError("need a Telegram bot token (123456:ABC...) and a chat id (or @channel)")
        return {"kind": "telegram", "token": token, "chat": chat}
    if kind == "ntfy":
        topic = str(raw.get("topic") or "").strip()
        if not NTFY_TOPIC_RE.match(topic):
            raise ValueError("an ntfy topic is 6-64 letters, digits, - or _")
        return {"kind": "ntfy", "topic": topic}
    raise ValueError("forward kind is discord, telegram or ntfy")


def _mask_forward(fw: dict[str, str] | None) -> dict[str, str] | None:
    if not fw:
        return None
    if fw["kind"] == "discord":
        return {"kind": "discord", "where": "Discord webhook …" + fw["url"][-6:]}
    if fw["kind"] == "telegram":
        return {"kind": "telegram", "where": f"Telegram chat {fw['chat']}"}
    return {"kind": "ntfy", "where": f"ntfy.sh/{fw['topic']}"}


def forward_text(payload: dict[str, Any]) -> str:
    lines = [str(payload.get("title") or ""), str(payload.get("body") or "")]
    if payload.get("url"):
        lines.append(str(payload["url"]))
    return "\n".join(x for x in lines if x).strip()


def forward_request(fw: dict[str, str] | None, payload: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """(url, request kwargs) for a forward, or None. Built only from a checked forward."""
    if not fw:
        return None
    text = forward_text(payload)
    if fw.get("kind") == "discord" and DISCORD_RE.match(fw.get("url", "")):
        return fw["url"], {"json": {"content": text[:1900], "allowed_mentions": {"parse": []}}}
    if fw.get("kind") == "telegram" and TELEGRAM_TOKEN_RE.match(fw.get("token", "")):
        return f"https://api.telegram.org/bot{fw['token']}/sendMessage", {
            "json": {"chat_id": fw["chat"], "text": text[:4000], "disable_web_page_preview": True}
        }
    if fw.get("kind") == "ntfy" and NTFY_TOPIC_RE.match(fw.get("topic", "")):
        return f"https://ntfy.sh/{fw['topic']}", {
            "data": text[:3500].encode(),
            "headers": {
                "Title": str(payload.get("title") or "")[:200].encode("ascii", "ignore").decode().strip()
            },
        }
    return None


def _kw_pattern(k: str) -> re.Pattern[str]:
    """Whole words, any case: "fda" matches "FDA approves", not "FDAX"; a phrase matches across
    any whitespace."""
    words = [re.escape(w) for w in k.split()]
    return re.compile(r"(?<![\w$])" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)


def _journal(t: dict[str, Any], body: dict[str, Any]) -> None:
    """A call's note and #tags (from the note, or a ``tags`` list): lowercase, at most 5."""
    if "note" in body:
        t["note"] = str(body.get("note") or "")[:200]
    tags = [str(x).lower().lstrip("#") for x in body.get("tags") or []]
    tags += TAG_RE.findall(str(body.get("note") or "").lower())
    if "note" in body or "tags" in body:
        t["tags"] = list(dict.fromkeys(x for x in tags if re.fullmatch(r"[a-z][a-z0-9-]{1,19}", x)))[:5]


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
