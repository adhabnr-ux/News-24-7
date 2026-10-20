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
* **stock_push**: a dial per stock: HIGH only, everything (past this phone's floor), or off.
* **focus**: a stretch (an hour, until the open) in which only CRITICAL alerts push.
* **burst**: past N alert pushes in 10 minutes, the rest fold into one summary.
* **reminders**: "remind me" on an alert: it comes back as a push in an hour, at the close or the open.
* **seen**: when (and at what price) each stock page was last opened, for "since you last looked".
* **sympathy**: when a peer of a stock you watch or hold jumps 4%+ on a HIGH alert and yours
  hasn't moved, one push (at most three a day).
* **restore**: merge a backup (the JSON export) into this desk, every part checked as if typed in.

Profiles live in SQLite (``profiles`` table) and, when ``STATE_DB`` is set, in the state database
too, so a fresh container on a free host keeps them.
"""

from __future__ import annotations

import json
import re
import secrets
import time
from datetime import datetime, timedelta
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
STOCK_PUSH_LEVELS = ("high", "all", "off")  # a stock with no entry is on "default"
MAX_STOCK_PUSH = 100
MAX_REMINDERS = 30
BURST_CHOICES = (0, 4, 6, 10)  # alert pushes per 10 minutes before the rest are folded into a summary
BURST_WINDOW_S = 600.0
BURST_WAIT_S = 300.0  # a folded alert waits this long for company before its summary goes out
MAX_SEEN = 150
REMIND_WHEN = {"1h": 3600.0, "3h": 10800.0}
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
            "port_alert": 0,  # %: push when the portfolio is up or down this much today (0 = off)
            "mute_sources": [],  # sources whose alerts never push to this phone
            "burst": 0,  # alert pushes per 10 min before the rest fold into one summary (0 = never)
            "sympathy": True,  # a peer of your stock jumps on news and yours hasn't moved: one push
            "account": 0.0,  # $: sizes the trade plans (0: the portfolio's value, if any)
            "risk_pct": 1.0,  # % of the account a plan risks on one idea
            "retest": True,  # second chance: a strong kind of news back at its alert price
            "edge_min": None,  # only push news whose catalyst record clears this: hit50 | hit60 | move2
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
        "kw_neg": [],  # words that keep a headline off this phone (never push), e.g. "crypto", "airdrop"
        "opened": [],  # alert ids recently opened (counted once)
        "follow": {"themes": [], "sectors": []},
        "stock_push": {},  # symbol -> "high" | "all" | "off" (absent: the phone's own settings)
        "cat_off": [],  # catalyst keys (catalysts.py) whose news never pushes to this phone
        "autopilots": [],  # paper forward-tests (autopilot.py): {"id", "catalyst", "band", "min_sev", "exit", "on"}
        "focus_until": 0.0,  # epoch: until then only CRITICAL alerts push
        "reminders": [],  # [{"id", "alert_id", "title", "tickers", "due", "created"}]
        "seen": {},  # symbol -> {"ts", "px"}: the last time its page was opened
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


EDGE_BARS = {  # an alert's catalyst record must clear this to push (when the phone sets one)
    "hit50": "went its way half the time or more by the close",
    "hit60": "went its way 60% of the time or more by the close",
    "move2": "typically moves 2% or more by the close, either way",
}


def edge_miss(cb: dict[str, Any] | None, bar: str) -> str:
    """Why a catalyst record falls short of a bar, in a few words; "" when it clears it (or has no
    record yet: a kind of news too new to judge isn't held back)."""
    close = (cb or {}).get("close")
    if not close:
        return ""
    if bar == "hit50" and close["hit"] < 0.5:
        return f"{round(close['hit'] * 100)}% its way, under 50%"
    if bar == "hit60" and close["hit"] < 0.6:
        return f"{round(close['hit'] * 100)}% its way, under 60%"
    if bar == "move2" and (cb.get("typical") or 0) < 2:
        return f"typical move ±{cb.get('typical') or 0:.1f}%, under 2%"
    return ""


def session_edge(kind: str, now: float, calendar: Any = None) -> float:
    """The next regular-session "open" (09:30 ET) or "close" (16:00, 13:00 on early-close days)
    after ``now`` (at least 30 s ahead), skipping weekends and the calendar's holidays."""
    holidays = set(getattr(calendar, "holidays", None) or ())
    early = set(getattr(calendar, "early", None) or ())
    day = datetime.fromtimestamp(now, ET)
    for i in range(14):
        d = (day + timedelta(days=i)).date()
        if d.weekday() >= 5 or d.isoformat() in holidays:
            continue
        h, m = (9, 30) if kind == "open" else ((13, 0) if d.isoformat() in early else (16, 0))
        t = datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp()
        if t > now + 30:
            return t
    return now + 86400.0


class Desk:
    def __init__(self, storage: Any, state: Any = None) -> None:
        self.calendar: Any = None  # the engine's Calendar (holidays), for "until the open"
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
            if want
            & (
                set(p.get("watchlist") or [])
                | set(p.get("holdings") or {})
                | {t for t, lv in (p.get("stock_push") or {}).items() if lv == "all"}
            )
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
        return self.block_reason(device, alert, now) is None

    def shared_list(self, code: str, now: float | None = None) -> dict[str, Any] | None:
        """A shared watchlist by its code (dashes and case don't matter): {symbols, name, until}, or
        None when the code is wrong or expired."""
        code = str(code or "").strip().upper().replace(" ", "").replace("-", "")
        if not code or not hasattr(self.storage, "get_setting"):
            return None
        raw = self.storage.get_setting(f"share:{code}")
        data = json.loads(raw) if raw else None
        if not data or data.get("until", 0) < (now or time.time()):
            return None
        syms = [x for x in (clean_symbol(s) for s in data.get("symbols") or []) if x]
        return {"symbols": syms, "name": data.get("name") or "", "until": data["until"]}

    def block_reason(self, device: str, alert: Any, now: float | None = None) -> str | None:
        """Why this phone's settings keep this alert from pushing, in plain words (None: they don't)."""
        b = self.block(device, alert, now)
        return b["text"] if b else None

    def block(self, device: str, alert: Any, now: float | None = None) -> dict[str, Any] | None:
        """The first of this phone's settings that keeps this alert from pushing: {code, text, fix},
        where fix is the desk action (and body) that would let it through. The same checks, in the
        same order, as `allows`."""
        p = self.profiles.get(device)
        if p is None:
            return None
        prefs = p.get("prefs") or {}
        cat = alert_category(alert)

        def out(code: str, text: str, label: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
            return {"code": code, "text": text, "fix": {"label": label, "action": action, "body": body}}

        if cat != "system" and not (prefs.get("cats") or {}).get(cat, True):
            return out(
                "category",
                f"you turned {cat} alerts off",
                f"Turn {cat} alerts on",
                "prefs",
                {"cats": {cat: True}},
            )
        sev_name = (
            alert.get("severity")
            if isinstance(alert, dict)
            else getattr(getattr(alert, "severity", None), "name", "")
        )
        sev = str(sev_name)
        t_now = now or time.time()
        tickers = [
            t
            for t in (
                (alert.get("tickers") if isinstance(alert, dict) else getattr(alert, "tickers", None)) or []
            )
            if t
        ]
        dial = self.stock_level(p, tickers)
        sym0 = tickers[0] if tickers else ""
        if dial == "off":
            return out(
                "stock_off",
                f"you set {sym0} to never push",
                f"Let {sym0} push again",
                "stock_push",
                {"symbol": sym0, "level": "default"},
            )
        if dial == "high" and SEV_RANK.get(sev, 0) < SEV_RANK["HIGH"]:
            return out(
                "stock_high",
                f"you set {sym0} to HIGH alerts only and this is {sev}",
                f"Push everything on {sym0}",
                "stock_push",
                {"symbol": sym0, "level": "all"},
            )
        if sev != "CRITICAL" and (p.get("focus_until") or 0) > t_now:
            return out(
                "focus",
                "focus mode is on (only CRITICAL comes through)",
                "End focus",
                "focus",
                {"minutes": 0},
            )
        if sev != "CRITICAL":
            if (p.get("snooze") or {}).get(cat, 0) > t_now:
                return out(
                    "snooze",
                    f"{cat} alerts were snoozed",
                    "End the snooze",
                    "snooze",
                    {"cat": cat, "hours": 0},
                )
            edge = (alert.get("edge") if isinstance(alert, dict) else getattr(alert, "edge", None)) or {}
            if dial != "all" and prefs.get("skip_fades") and (edge.get("base") or {}).get("fades"):
                return out("fades", "alerts like this usually fade, and you skip those",
                           "Stop skipping fades", "prefs", {"skip_fades": False})  # fmt: skip
        if self.all_muted(device, alert, now):
            tickers = alert.get("tickers") if isinstance(alert, dict) else getattr(alert, "tickers", None)
            sym = (tickers or [""])[0]
            return out(
                "muted", f"you muted {sym or 'this stock'}", f"Unmute {sym}", "unmute", {"symbol": sym}
            )
        if sev != "CRITICAL":
            if self.source_muted(p, alert):
                src = _source_of(alert)
                return out("source", f"you muted its source ({src})", f"Unmute {src}", "prefs",
                           {"mute_source": src, "on": False})  # fmt: skip
            edge = (alert.get("edge") if isinstance(alert, dict) else getattr(alert, "edge", None)) or {}
            kind = edge.get("catalyst") or {}
            if kind.get("key") and kind["key"] in (p.get("cat_off") or []) and dial != "all":
                return out("catalyst_off", f"you turned off {kind['label'].lower()} news", f"Push {kind['label'].lower()} news again",
                           "cat_mute", {"key": kind["key"], "on": False})  # fmt: skip
            bar = prefs.get("edge_min")
            if bar in EDGE_BARS and kind.get("key") and dial != "all":
                miss = edge_miss(edge.get("cat_base"), bar)
                if miss:
                    return out("edge", f"{kind['label'].lower()} news has a record under your bar ({miss})",
                               "Push every record", "prefs", {"edge_min": None})  # fmt: skip
            word = self.neg_match(device, alert)
            if word:
                return out("word", f"it has your never-push word “{word}”", f"Remove “{word}”",
                           "neg_remove", {"keyword": word})  # fmt: skip
        floor = prefs.get("min_sev")
        if dial != "all" and cat != "system" and floor in SEV_RANK and SEV_RANK.get(sev, 0) < SEV_RANK[floor]:
            return out(
                "floor",
                f"it's {sev}, under your {floor} floor",
                "Remove the floor",
                "prefs",
                {"min_sev": None},
            )
        if in_quiet(prefs.get("quiet"), now) and sev != "CRITICAL":
            return out(
                "quiet", "it came in your quiet hours", "Turn quiet hours off", "prefs", {"quiet": None}
            )
        return None

    @staticmethod
    def stock_level(p: dict[str, Any], tickers: list[str]) -> str | None:
        """The per-stock dial that applies to an alert about these stocks: "off" when every one of
        them is off, "all" when any is set to everything, "high" when every one is HIGH-only."""
        dial = p.get("stock_push") or {}
        if not dial or not tickers:
            return None
        got = [dial.get(t) for t in tickers]
        if "all" in got:
            return "all"
        if all(g == "off" for g in got):
            return "off"
        if all(g == "high" for g in got):
            return "high"
        return None

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
        self._queue(p, get, sev, now)
        return True

    def _queue(self, p: dict[str, Any], get: Any, sev: str, now: float | None) -> None:
        q = p.setdefault("digest_q", [])
        aid = str(get("id") or "")
        if aid and any(x["id"] == aid for x in q):
            return
        q.append({"id": aid, "title": str(get("title") or "")[:160], "severity": sev, "tickers": list(get("tickers") or [])[:4],
                  "ts": now or time.time()})  # fmt: skip
        del q[:-50]
        self._dirty = True

    def burst_hold(self, device: str, alert: Any, recent: int, now: float | None = None) -> bool:
        """Burst protection: a phone that already got ``burst`` alert pushes in the last 10 minutes
        has the rest folded into one summary (sent a few minutes later). CRITICAL always comes."""
        p = self.profiles.get(device)
        n = int(((p or {}).get("prefs") or {}).get("burst") or 0)
        if not p or n <= 0 or recent < n:
            return False
        get = (lambda k: alert.get(k)) if isinstance(alert, dict) else (lambda k: getattr(alert, k, None))
        sev = get("severity")
        sev = sev if isinstance(sev, str) else getattr(sev, "name", "")
        if sev == "CRITICAL":
            return False
        self._queue(p, get, sev, now)
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
            if (
                not mins
                and (p.get("prefs") or {}).get("burst")
                and now - min(x["ts"] for x in q) < BURST_WAIT_S
            ):
                continue  # folded by burst protection: wait a few minutes for the rest of the rush
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

    @staticmethod
    def source_muted(p: dict[str, Any], alert: Any) -> bool:
        muted = (p.get("prefs") or {}).get("mute_sources") or []
        if not muted:
            return False
        src = _source_of(alert)
        return bool(src) and src in muted

    def neg_match(self, device: str, alert_or_text: Any) -> str | None:
        """The first of this phone's never-push words in the alert's title (or a text), else None."""
        neg = (self.profiles.get(device) or {}).get("kw_neg") or []
        if not neg:
            return None
        if isinstance(alert_or_text, str):
            text = alert_or_text
        elif isinstance(alert_or_text, dict):
            text = str(alert_or_text.get("title") or "")
        else:
            text = str(getattr(alert_or_text, "title", "") or "")
        return next((k for k in neg if _kw_pattern(k).search(text)), None)

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
                "sympathy",
                "retest",
            ):
                if flag in body:
                    prefs[flag] = bool(body[flag])
            if "port_alert" in body:
                try:
                    v = float(body.get("port_alert") or 0)
                except (TypeError, ValueError):
                    v = -1
                if v not in (0, 1, 2, 3, 5):
                    raise ValueError("port_alert is 0 (off), 1, 2, 3 or 5")
                prefs["port_alert"] = v
            if "account" in body:
                try:
                    v = float(body.get("account") or 0)
                except (TypeError, ValueError):
                    v = -1
                if not 0 <= v <= 1e10:
                    raise ValueError("account size is a dollar amount (0 clears it)")
                prefs["account"] = v
            if "risk_pct" in body:
                try:
                    v = float(body.get("risk_pct"))
                except (TypeError, ValueError):
                    v = -1
                if v not in (0.25, 0.5, 1.0, 2.0):
                    raise ValueError("risk per idea is 0.25, 0.5, 1 or 2% of the account")
                prefs["risk_pct"] = v
            if "edge_min" in body:
                bar = body.get("edge_min")
                if bar not in (None, "", *EDGE_BARS):
                    raise ValueError("edge bar is hit50, hit60, move2 or off")
                prefs["edge_min"] = bar or None
            if "burst" in body:
                try:
                    n = int(body.get("burst") or 0)
                except (TypeError, ValueError):
                    n = -1
                if n not in BURST_CHOICES:
                    raise ValueError("burst is 0 (off), 4, 6 or 10 pushes per 10 minutes")
                prefs["burst"] = n
            if "mute_source" in body:
                src = str(body.get("mute_source") or "").strip()[:60]
                ms = [x for x in prefs.get("mute_sources") or [] if x != src]
                if src and body.get("on", True):
                    ms.append(src)
                prefs["mute_sources"] = ms[:50]
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
            data = self.shared_list(str(body.get("code") or ""), now)
            if data is None:
                raise ValueError("that list code is wrong or expired")
            syms = data["symbols"]
            before = set(p["watchlist"])
            p["watchlist"] = list(dict.fromkeys([*p["watchlist"], *syms]))[:MAX_WATCH]
            p["imported"] = len(set(p["watchlist"]) - before)
            if data.get("name"):
                for x in syms:
                    p.setdefault("groups", {}).setdefault(x, data["name"][:24])
        elif action == "hold_import":
            n = 0
            for line in str(body.get("text") or "").splitlines()[:200]:
                parts = [x for x in re.split(r"[,;\t ]+", line.strip()) if x]
                if not parts or parts[0].lower() in ("symbol", "ticker"):
                    continue
                sym = clean_symbol(parts[0])
                try:
                    shares = float(parts[1].replace("$", "")) if len(parts) > 1 else 0.0
                    cost = float(parts[2].replace("$", "")) if len(parts) > 2 else None
                except ValueError:
                    continue
                if not sym or shares <= 0 or (cost is not None and cost <= 0):
                    continue
                h = p.setdefault("holdings", {})
                if len(h) >= 100 and sym not in h:
                    break
                h[sym] = {"shares": shares, "cost": cost}
                n += 1
            if not n:
                raise ValueError("no lines like 'AAPL 10 150' (ticker, shares, optional cost) found")
            p["imported"] = n
        elif action in ("neg_add", "neg_remove"):
            k = re.sub(r"\s+", " ", str(body.get("keyword") or "")).strip().lower()
            neg = [x for x in p.get("kw_neg") or [] if x != k]
            if action == "neg_add":
                if not 2 <= len(k) <= 40:
                    raise ValueError("a word is 2 to 40 characters")
                if len(neg) >= 30:
                    raise ValueError("at most 30 never-push words")
                neg.append(k)
            p["kw_neg"] = neg
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
            until = str(body.get("until") or "")
            try:
                if until in ("open", "close"):  # until the next session open / close
                    hours = min(MAX_MUTE_H, (session_edge(until, now, self.calendar) - now) / 3600)
                else:
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
        elif action == "stock_push":
            sym = clean_symbol(body.get("symbol"))
            level = str(body.get("level") or "default")
            if not sym or level not in (*STOCK_PUSH_LEVELS, "default"):
                raise ValueError("need a symbol and a level: default, high, all or off")
            dial = p.setdefault("stock_push", {})
            if level == "default":
                dial.pop(sym, None)
            else:
                if sym not in dial and len(dial) >= MAX_STOCK_PUSH:
                    raise ValueError(f"at most {MAX_STOCK_PUSH} stocks with their own setting")
                dial[sym] = level
        elif action == "focus":
            until = str(body.get("until") or "")
            try:
                minutes = float(body.get("minutes") or 0)
            except (TypeError, ValueError):
                raise ValueError("minutes must be a number") from None
            if until == "open":
                end = session_edge("open", now, self.calendar)
            elif 0 <= minutes <= 24 * 60:
                end = now + minutes * 60 if minutes else 0.0
            else:
                raise ValueError("focus is 0 (off) to 1440 minutes, or until the open")
            p["focus_until"] = end
        elif action == "remind":
            aid = str(body.get("id") or "")[:40]
            when = str(body.get("when") or "")
            alert = self.storage.get_alert(aid) if aid and hasattr(self.storage, "get_alert") else None
            if alert is None:
                raise ValueError("no such alert")
            if when in REMIND_WHEN:
                due = now + REMIND_WHEN[when]
            elif when in ("open", "close"):
                due = session_edge(when, now, self.calendar)
            else:
                raise ValueError("remind me in 1h, 3h, at the open or at the close")
            rem = [r for r in p.get("reminders") or [] if r["alert_id"] != aid]
            if len(rem) >= MAX_REMINDERS:
                raise ValueError(f"at most {MAX_REMINDERS} reminders")
            rem.append({"id": secrets.token_hex(4), "alert_id": aid, "title": str(alert.get("title") or "")[:160],
                        "tickers": [t for t in alert.get("tickers") or [] if t][:3], "due": due, "created": now})  # fmt: skip
            p["reminders"] = sorted(rem, key=lambda r: r["due"])
        elif action == "remind_clear":
            rid = str(body.get("id") or "")
            p["reminders"] = [r for r in p.get("reminders") or [] if r["id"] != rid and r["alert_id"] != rid]
        elif action == "viewed":
            sym = clean_symbol(body.get("symbol"))
            if not sym:
                raise ValueError("no valid symbol")
            q = price_of(sym) if price_of else None
            seen = p.setdefault("seen", {})
            seen.pop(sym, None)
            seen[sym] = {"ts": now, "px": q[1] if q else None}
            for old in list(seen)[:-MAX_SEEN]:
                seen.pop(old, None)
        elif action == "cat_mute":
            from .catalysts import LABELS

            key = str(body.get("key") or "")
            if key not in LABELS:
                raise ValueError("not a kind of news Foretape reads")
            off = [k for k in p.get("cat_off") or [] if k != key]
            if body.get("on", True):
                off.append(key)
            p["cat_off"] = off
        elif action == "autopilot_add":
            from .autopilot import MAX_RULES as MAX_AUTOPILOTS
            from .autopilot import clean_rule

            rules = p.setdefault("autopilots", [])
            if len(rules) >= MAX_AUTOPILOTS:
                raise ValueError(f"at most {MAX_AUTOPILOTS} autopilots")
            rule = clean_rule(body, now)
            same = ("catalyst", "band", "min_sev", "exit")
            if any(all(r[k] == rule[k] for k in same) for r in rules):
                raise ValueError("that autopilot is already running")
            rules.append(rule)
        elif action in ("autopilot_remove", "autopilot_toggle"):
            rid = str(body.get("id") or "")
            rules = p.get("autopilots") or []
            r = next((x for x in rules if x["id"] == rid), None)
            if r is None:
                raise ValueError("no such autopilot")
            if action == "autopilot_remove":
                p["autopilots"] = [x for x in rules if x["id"] != rid]
            else:
                r["on"] = not r.get("on", True)
        elif action == "restore":
            p["restored"] = self._restore(device, p, body.get("desk") or body, bool(body.get("prefs")), now)
        else:
            raise ValueError(f"unknown action {action!r}")
        self.save(device)
        return p

    def due_reminders(self, now: float | None = None) -> list[tuple[str, dict[str, Any]]]:
        """(device, reminder) for every "remind me" that has come due; each is dropped as it is returned."""
        now = now or time.time()
        out = []
        for device, p in self.profiles.items():
            rem = p.get("reminders") or []
            due = [r for r in rem if r["due"] <= now]
            if not due:
                continue
            p["reminders"] = [r for r in rem if r["due"] > now]
            out.extend((device, r) for r in due)
            self.save(device)
        return out

    def _restore(
        self, device: str, p: dict[str, Any], data: Any, with_prefs: bool, now: float
    ) -> dict[str, int]:
        """Merge a backup (the app's JSON export) into this desk. Nothing is replaced: stocks,
        alerts, words and rules are added when missing, and every one goes through the same checks
        as if it were typed in. Returns how many of each were added."""
        if not isinstance(data, dict):
            raise ValueError("that isn't a Foretape desk backup")
        if isinstance(data.get("desk"), dict):
            data = data["desk"]
        if not any(
            k in data for k in ("watchlist", "price_alerts", "tracked", "holdings", "keywords", "rules")
        ):
            raise ValueError("that isn't a Foretape desk backup")
        n = dict.fromkeys(
            (
                "stocks",
                "alerts",
                "holdings",
                "keywords",
                "words",
                "notes",
                "rules",
                "calls",
                "follows",
                "dials",
            ),
            0,
        )

        def lst(key: str, cap: int = 100) -> list[Any]:
            v = data.get(key)
            return v[:cap] if isinstance(v, list) else []

        def dct(key: str, cap: int = 150) -> dict[str, Any]:
            v = data.get(key)
            return dict(list(v.items())[:cap]) if isinstance(v, dict) else {}

        def go(action: str, body: dict[str, Any]) -> bool:
            try:
                self.apply(device, action, body)
                return True
            except (ValueError, TypeError, KeyError, AttributeError):
                return False

        have = set(p["watchlist"])
        fresh = [s for s in (clean_symbol(x) for x in lst("watchlist", MAX_WATCH)) if s and s not in have]
        for sym in fresh:  # one at a time so auto-move alerts and the cap behave as when typed
            if go("watch_add", {"symbol": sym}):
                n["stocks"] += 1
        for sym, g in dct("groups").items():
            if isinstance(g, str):
                go("watch_group", {"symbol": sym, "group": g})
        for sym, h in dct("holdings", 100).items():
            if (
                sym not in p.get("holdings", {})
                and isinstance(h, dict)
                and go("hold_set", {"symbol": sym, "shares": h.get("shares"), "cost": h.get("cost")})
            ):
                n["holdings"] += 1  # fmt: skip
        have_pa = {(a["symbol"], a["kind"], a["value"]) for a in p["price_alerts"] if not a.get("fired")}
        for a in lst("price_alerts", MAX_PRICE_ALERTS):
            if not isinstance(a, dict) or a.get("fired"):
                continue
            try:
                key = (clean_symbol(a.get("symbol")), str(a.get("kind")), float(a.get("value")))
            except (TypeError, ValueError):
                continue
            if key not in have_pa and go(
                "alert_add",
                {
                    "symbol": a.get("symbol"),
                    "kind": a.get("kind"),
                    "value": a.get("value"),
                    "rearm": a.get("rearm"),
                },
            ):
                have_pa.add(key)
                n["alerts"] += 1  # fmt: skip
        have_kw = {k["k"] for k in p.get("keywords") or []}
        for k in lst("keywords", MAX_KEYWORDS):
            word = str(k.get("k") if isinstance(k, dict) else k or "").strip().lower()
            if word and word not in have_kw and go("kw_add", {"keyword": word}):
                have_kw.add(word)
                n["keywords"] += 1
        for w in lst("kw_neg", 30):
            if str(w).strip().lower() not in (p.get("kw_neg") or []) and go("neg_add", {"keyword": w}):
                n["words"] += 1
        for sym, note in dct("notes").items():
            sym = clean_symbol(sym)
            if (
                sym
                and sym not in p.get("notes", {})
                and isinstance(note, dict)
                and go("note", {"symbol": sym, "text": note.get("text")})
            ):
                n["notes"] += 1  # fmt: skip
        have_rules = {(r["kind"], r["symbol"], r["value"], r["dir"]) for r in p.get("rules") or []}
        for r in lst("rules", MAX_RULES):
            if not isinstance(r, dict):
                continue
            try:
                key = (
                    r.get("kind"),
                    "*" if r.get("symbol") == "*" else clean_symbol(r.get("symbol")),
                    float(r.get("value") or 0),
                    r.get("dir") or "",
                )
            except (TypeError, ValueError):
                continue
            if key not in have_rules and go(
                "rule_add",
                {
                    "kind": r.get("kind"),
                    "symbol": r.get("symbol"),
                    "value": r.get("value"),
                    "dir": r.get("dir"),
                },
            ):
                have_rules.add(key)
                n["rules"] += 1  # fmt: skip
        f = data.get("follow") if isinstance(data.get("follow"), dict) else {}
        for kind in ("themes", "sectors"):
            for name in (f.get(kind) or [])[:30]:
                if name not in (p.get("follow") or {}).get(kind, []) and go(
                    "follow", {"kind": kind, "name": name}
                ):
                    n["follows"] += 1
        for key in lst("cat_off", 40):
            if isinstance(key, str) and key not in (p.get("cat_off") or []):
                go("cat_mute", {"key": key})
        for sym, lv in dct("stock_push", MAX_STOCK_PUSH).items():
            if sym not in p.get("stock_push", {}) and go("stock_push", {"symbol": sym, "level": lv}):
                n["dials"] += 1
        for src in (
            (data.get("prefs") or {}).get("mute_sources") or [] if isinstance(data.get("prefs"), dict) else []
        ):
            go("prefs", {"mute_source": src})
        # paper calls and the journal: added when their id is new, with every field checked
        ids = {t["id"] for t in [*p["tracked"], *p["closed"]]}

        def call(t: Any, closed: bool) -> dict[str, Any] | None:
            if not isinstance(t, dict):
                return None
            sym, d = clean_symbol(t.get("symbol")), t.get("direction")
            try:
                entry = round(float(t.get("entry")), 4)
            except (TypeError, ValueError):
                return None
            if not sym or d not in ("long", "short") or entry <= 0:
                return None

            def num(k: str) -> float | None:
                try:
                    v = float(t[k]) if t.get(k) is not None else None
                except (TypeError, ValueError):
                    return None
                return v if v is not None and v > 0 else None

            tid = str(t.get("id") or "")
            out = {"id": tid if re.fullmatch(r"[0-9a-f]{10}", tid) and tid not in ids else secrets.token_hex(5),
                   "symbol": sym, "direction": d, "entry": entry, "ts": float(t.get("ts") or now),
                   "stop": num("stop"), "target": num("target"),
                   "tags": [x for x in (t.get("tags") or []) if isinstance(x, str) and re.fullmatch(r"[a-z][a-z0-9-]{1,19}", x)][:5],
                   "note": str(t.get("note") or "")[:200]}  # fmt: skip
            if closed:
                try:
                    out.update(
                        exit=num("exit"),
                        closed=float(t.get("closed") or now),
                        pnl_pct=float(t["pnl_pct"]) if t.get("pnl_pct") is not None else None,
                    )
                except (TypeError, ValueError):
                    return None
                if t.get("lesson"):
                    out["lesson"] = str(t["lesson"])[:200]
            ids.add(out["id"])
            return out

        for t in lst("tracked", MAX_TRACKED):
            c = call(t, False)
            if (
                c
                and len(p["tracked"]) < MAX_TRACKED
                and not any(x["id"] == (t.get("id") if isinstance(t, dict) else None) for x in p["tracked"])
            ):
                p["tracked"].append(c)
                n["calls"] += 1  # fmt: skip
        for t in lst("closed", 50):
            c = call(t, True)
            if (
                c
                and len(p["closed"]) < 50
                and not any(x["id"] == (t.get("id") if isinstance(t, dict) else None) for x in p["closed"])
            ):
                p["closed"].append(c)
                n["calls"] += 1  # fmt: skip
        if with_prefs and isinstance(data.get("prefs"), dict):
            src = data["prefs"]
            body = {
                k: src[k]
                for k in (
                    "eod",
                    "earn_remind",
                    "open_plan",
                    "week",
                    "skip_fades",
                    "close_recap",
                    "tomorrow",
                    "followups",
                    "sympathy",
                    "retest",
                    "watch_push",
                    "min_sev",
                    "digest",
                    "port_alert",
                    "auto_move",
                    "burst",
                    "account",
                    "risk_pct",
                    "edge_min",
                    "quiet",
                )
                if k in src
            }
            if isinstance(src.get("cats"), dict):
                body["cats"] = src["cats"]
            go("prefs", body)  # a bad value drops the whole prefs block, not the rest of the restore
        return n

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
            "kw_neg": p.get("kw_neg") or [],
            "imported": p.pop("imported", None),
            "follow": p.get("follow") or {"themes": [], "sectors": []},
            "stock_push": p.get("stock_push") or {},
            "cat_off": p.get("cat_off") or [],
            "autopilots": _autopilots_view(p),
            "focus": p["focus_until"] if (p.get("focus_until") or 0) > now else None,
            "reminders": sorted(p.get("reminders") or [], key=lambda r: r["due"]),
            "restored": p.pop("restored", None),
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


def _source_of(alert: Any) -> str:
    item = alert.get("item") if isinstance(alert, dict) else getattr(alert, "item", None)
    return str(
        (item.get("source") if isinstance(item, dict) else getattr(item, "source", "")) if item else ""
    )


def _kw_pattern(k: str) -> re.Pattern[str]:
    """Whole words, any case: "fda" matches "FDA approves", not "FDAX"; a phrase matches across
    any whitespace."""
    words = [re.escape(w) for w in k.split()]
    return re.compile(r"(?<![\w$])" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)


def _autopilots_view(p: dict[str, Any]) -> list[dict[str, Any]]:
    from .autopilot import rule_stats

    return [{**r, "stats": rule_stats(p, r["id"])} for r in p.get("autopilots") or []]


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
