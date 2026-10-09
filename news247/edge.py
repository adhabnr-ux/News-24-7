"""The edge desk: what a trader wants next to a headline, beyond "this is important".

For every alert:

* **The play**: which way it cuts, how convicted the scorer is, the names hit directly, and
  the read-through names (suppliers, competitors, the sector basket).
* **Precedents**: the most similar market-moving events on record
  (``data/history.yaml``, 150+ events since 2016), with what the stocks did then. Precedents
  are matched by the scorer's own themes, entities and tickers, so "FHFA posts about
  VantageScore" finds both 2026 FICO crashes.
* **Your edge**: how long after Foretape the same story reached mainstream outlets (CNBC,
  Yahoo, Google News, MarketWatch...). It's measured live, story by story.
* **The tape since**: each direct ticker's move since the alert, from the price feed.

Plus the **Brief** (what happened overnight, what's scheduled, what's moving) and the
**calendar** of scheduled catalysts (``data/calendar.yaml`` plus generated option
expirations).
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .analysis.scorer import Scorer
from .backtest import headline_item
from .config import load_package_yaml
from .models import Alert, Analysis

ET = ZoneInfo("America/New_York")

# Where "everyone else" hears about it. A story reaching one of these after Foretape alerted
# is the measured lead. (Prefixes of source names in default_sources.yaml.)
MAINSTREAM_PREFIXES = (
    "cnbc-",
    "yahoo-",
    "gnews-",
    "mw-",
    "wsj-",
    "bloomberg-",
    "nyt-",
    "fortune",
    "axios",
    "hackernews",
)

# How history.yaml's "move" lines name the market-wide tickers
MOVE_ALIASES = {
    "SPY": ("S&P", "S&P 500"),
    "QQQ": ("Nasdaq", "NDX", "Nasdaq-100"),
    "DIA": ("Dow",),
    "IWM": ("Russell", "Russell 2000"),
    "USO": ("Oil", "Brent", "WTI", "crude"),
    "XLE": ("Oil", "Brent", "WTI"),
    "SMH": ("SOX",),
    "GLD": ("Gold",),
}

CONVICTION = ((85, "Maximum"), (75, "High"), (55, "Elevated"), (35, "Watch"), (0, "Low"))


def conviction(score: float) -> tuple[str, int]:
    """('High', 4): a label and a 1-5 meter for a 0-100 score."""
    label = next(lbl for floor, lbl in CONVICTION if score >= floor)
    return label, max(1, min(5, round(score / 20)))


@dataclass
class _Precedent:
    event: dict[str, Any]
    headline: str
    themes: set[str]
    entities: set[str]
    tickers: set[str]


@dataclass
class _Tracked:
    alert_id: str
    created: float
    symbols: list[str]
    refs: dict[str, float] = field(default_factory=dict)


class EdgeDesk:
    def __init__(self, scorer: Scorer, history: dict[str, Any] | None = None) -> None:
        self.scorer = scorer
        self._history = history
        self._precedents: list[_Precedent] | None = None
        self.tracked: dict[str, _Tracked] = {}

    def warm(self) -> int:
        """Score the history now (the engine calls this in a thread at start-up)."""
        return len(self.precedents)

    @property
    def ready(self) -> bool:
        return self._precedents is not None

    @property
    def precedents(self) -> list[_Precedent]:
        """History scored with the live rules (~0.5 s for 150 events: the engine warms this in a
        background thread at start-up so no alert ever waits for it)."""
        if self._precedents is None:
            history = self._history if self._history is not None else load_package_yaml("history.yaml")
            self._precedents = []
            for ev in history.get("events", []):
                themes: set[str] = set()
                entities: set[str] = set()
                best, best_score = "", -1.0
                for h in ev.get("first_reports", []):
                    an = self.scorer.score(headline_item(h))
                    themes.update(an.themes)
                    entities.update(an.entities)
                    if an.score > best_score:
                        best, best_score = (h["text"] if isinstance(h, dict) else h), an.score
                self._precedents.append(
                    _Precedent(ev, best, themes, entities, {t.upper() for t in ev.get("tickers", [])})
                )
        return self._precedents

    # ------------------------------------------------------------------ the play

    def play(self, alert: Alert) -> dict[str, Any]:
        an = alert.analysis
        if an is None:
            return {}
        direct: list[str] = []
        for t in alert.item.tickers if alert.item else []:
            direct.append(t.upper())
        for name in an.entities:
            comp = self.scorer.entities.company(name)
            if comp is not None and comp.ticker:
                direct.append(comp.ticker)
        if alert.item is not None:
            direct.extend(self.scorer.entities.find_tickers(alert.item.text, alert.item.title))
        direct = list(dict.fromkeys(direct))[:6]
        read_through = [t for t in an.tickers if t not in direct][:8]
        if not direct:  # macro news: the market-wide names lead
            direct, read_through = read_through[:4], read_through[4:]
        label, meter = conviction(an.score)
        direction = an.direction
        if alert.item is not None and alert.item.source.startswith(("polymarket", "kalshi")):
            direction = "mixed"  # "odds jump" says nothing about which way stocks go; precedents may
        return {
            "direction": direction,
            "conviction": label,
            "meter": meter,
            "score": an.score,
            "direct": direct,
            "read_through": read_through,
            "themes": [t.replace("_", " ") for t in an.themes],
        }

    # ------------------------------------------------------------------ precedents

    def precedents_for(self, an: Analysis, title: str = "", limit: int = 3) -> list[dict[str, Any]]:
        themes, entities, tickers = set(an.themes), set(an.entities), set(an.tickers)
        scored: list[tuple[float, str, _Precedent]] = []
        for p in self.precedents:
            if title and p.headline == title:
                continue  # the event itself, replayed (tests, demos)
            shared_t, shared_e = themes & p.themes, entities & p.entities
            if not shared_t and not shared_e:
                continue
            sim = 3.0 * len(shared_t) + 2.0 * len(shared_e) + min(3, len(tickers & p.tickers))
            if sim < 4:
                continue
            scored.append((sim, str(p.event.get("date", "")), p))
        scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
        out = []
        for sim, _, p in scored[:limit]:
            ev = p.event
            out.append(
                {
                    "date": str(ev.get("date", "")),
                    "headline": p.headline,
                    "move": ev.get("move", ""),
                    "first_source": ev.get("first_source", ""),
                    "category": str(ev.get("category", "")).replace("_", " "),
                    "tickers": ev.get("tickers", [])[:6],
                    "similarity": sim,
                }
            )
        return out

    @staticmethod
    def precedent_direction(precs: list[dict[str, Any]], tickers: list[str]) -> str:
        """Which way similar events moved the names that matter ('down' / 'up' / '')."""
        votes = 0.0
        for p in precs:
            move = str(p.get("move", ""))
            sign = ""
            for t in tickers:
                for name in (t, *MOVE_ALIASES.get(t, ())):
                    m = re.search(rf"(?<![\w$]){re.escape(name)}\s+(?:~\s*)?([+\-−])\d", move)
                    if m:
                        sign = m.group(1)
                        break
                if sign:
                    break
            if not sign:
                m = re.search(r"(?:^|[\s(])([+\-−])\d", move)
                sign = m.group(1) if m else ""
            if sign:
                votes += p.get("similarity", 1) * (1 if sign == "+" else -1)
        return "up" if votes > 0 else "down" if votes < 0 else ""

    # ------------------------------------------------------------------ annotate an alert

    def annotate(self, alert: Alert, last_price: dict[str, tuple[float, float]] | None = None) -> None:
        """Fill ``alert.edge`` before it is stored and pushed."""
        if alert.kind != "news" or alert.analysis is None:
            return
        play = self.play(alert)
        alert.edge["play"] = play
        # never delay an alert: until the history is scored (first seconds after start) skip it
        precs = self.precedents_for(alert.analysis, alert.title) if self.ready else []
        alert.edge["precedents"] = precs
        if play["direction"] not in ("up", "down"):  # ambiguous headline: ask history
            lean = self.precedent_direction(precs, play["direct"])
            if lean:
                play["direction"], play["direction_basis"] = lean, "precedents"
        if alert.item is not None:
            alert.edge["first_source"] = alert.item.source
            if alert.item.latency is not None:
                alert.edge["caught_after_s"] = round(alert.item.latency, 1)
        symbols = play.get("direct", [])[:4]
        if symbols:
            t = _Tracked(alert.id, alert.created, symbols)
            for sym in symbols:
                last = (last_price or {}).get(sym)
                if last and alert.created - last[0] < 600:  # fresh quote: this is the reference
                    t.refs[sym] = last[1]
            alert.edge["refs"] = dict(t.refs)
            self.tracked[alert.id] = t

    def fill_refs(self, price_at: Any, now: float | None = None) -> dict[str, dict[str, float]]:
        """For alerts whose tickers weren't quoted yet: take the first price at/after the alert.
        Returns {alert_id: new refs} to persist; forgets alerts older than 6 hours."""
        now = now or time.time()
        updates: dict[str, dict[str, float]] = {}
        for aid, t in list(self.tracked.items()):
            if now - t.created > 6 * 3600:
                del self.tracked[aid]
                continue
            new = {}
            for sym in t.symbols:
                if sym in t.refs:
                    continue
                p = price_at(sym, t.created, 900)
                if p:
                    t.refs[sym] = new[sym] = p
            if new:
                updates[aid] = dict(t.refs)
        return updates

    @staticmethod
    def since(refs: dict[str, float], last_price: dict[str, tuple[float, float]]) -> dict[str, float]:
        out = {}
        for sym, ref in (refs or {}).items():
            last = last_price.get(sym)
            if last and ref:
                out[sym] = round((last[1] / ref - 1.0) * 100.0, 2)
        return out

    # ------------------------------------------------------------------ lead over mainstream

    @staticmethod
    def is_mainstream(source: str) -> bool:
        return source.startswith(MAINSTREAM_PREFIXES)

    # ------------------------------------------------------------------ push copy

    @staticmethod
    def push_line(alert: Alert) -> str:
        """One line for the lock screen: the strongest precedent."""
        precs = alert.edge.get("precedents") or []
        if not precs:
            return ""
        p = precs[0]
        when = _short_date(p["date"])
        return f"Last time ({when}): {p['move']}"[:160]


def _short_date(d: str) -> str:
    try:
        dt = datetime.strptime(d, "%Y-%m-%d")
        return f"{dt:%b} {dt.day}, {dt.year}"
    except ValueError:
        return d


# --------------------------------------------------------------------------- calendar


def _third_friday(year: int, month: int) -> date:
    d = date(year, month, 15)
    return d + timedelta(days=(4 - d.weekday()) % 7)


class Calendar:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        data = data if data is not None else load_package_yaml("calendar.yaml")
        self.events = [dict(e) for e in data.get("events", [])]
        self.holidays = {str(d) for d in data.get("market_holidays", [])}
        self.early = {str(d) for d in data.get("early_closes", [])}

    def upcoming(self, days: int = 45, now: float | None = None) -> list[dict[str, Any]]:
        now_dt = datetime.fromtimestamp(now or time.time(), ET)
        today = now_dt.date()
        end = today + timedelta(days=days)
        out: list[dict[str, Any]] = []
        for e in self.events:
            d = e["date"] if isinstance(e["date"], date) else date.fromisoformat(str(e["date"]))
            if today <= d <= end:
                out.append({**e, "date": d.isoformat()})
        # option expirations
        y, m = today.year, today.month
        for _ in range(3):
            d = _third_friday(y, m)
            if str(d) in self.holidays:
                d -= timedelta(days=1)
            if today <= d <= end:
                quad = m in (3, 6, 9, 12)
                out.append(
                    {
                        "date": d.isoformat(),
                        "time": "16:00",
                        "title": "Quad witching (quarterly options + futures expiry)"
                        if quad
                        else "Monthly options expiration",
                        "kind": "options",
                        "impact": 2 if quad else 1,
                    }
                )
            y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        for d in sorted(self.holidays):
            dd = date.fromisoformat(d)
            if today <= dd <= end:
                out.append({"date": d, "title": "US markets closed", "kind": "market", "impact": 1})
        for d in sorted(self.early):
            dd = date.fromisoformat(d)
            if today <= dd <= end:
                out.append(
                    {
                        "date": d,
                        "time": "13:00",
                        "title": "US markets close early",
                        "kind": "market",
                        "impact": 1,
                    }
                )
        out.sort(key=lambda e: (e["date"], e.get("time") or "00:00"))
        for e in out:
            when = datetime.fromisoformat(f"{e['date']}T{e.get('time') or '09:30'}").replace(tzinfo=ET)
            e["ts"] = when.timestamp()
            e["in_days"] = (date.fromisoformat(e["date"]) - today).days
        return out

    def market_status(self, now: float | None = None) -> dict[str, Any]:
        """NYSE regular session: open/closed, and seconds until the next open or close."""
        now_dt = datetime.fromtimestamp(now or time.time(), ET)

        def session(d: date) -> tuple[datetime, datetime] | None:
            if d.weekday() >= 5 or d.isoformat() in self.holidays:
                return None
            close_h = 13 if d.isoformat() in self.early else 16
            return (
                datetime(d.year, d.month, d.day, 9, 30, tzinfo=ET),
                datetime(d.year, d.month, d.day, close_h, 0, tzinfo=ET),
            )

        today = session(now_dt.date())
        if today and today[0] <= now_dt < today[1]:
            return {"open": True, "phase": "open", "until_s": (today[1] - now_dt).total_seconds()}
        if today and now_dt < today[0]:
            phase = "pre-market" if now_dt.hour >= 4 else "closed"
        elif today and now_dt.hour < 20:
            phase = "after-hours"
        else:
            phase = "closed"
        d = now_dt.date()
        for i in range(0, 12):
            s = session(d + timedelta(days=i))
            if s and s[0] > now_dt:
                return {"open": False, "phase": phase, "until_s": (s[0] - now_dt).total_seconds()}
        return {"open": False, "phase": phase, "until_s": None}

    @staticmethod
    def last_close(now: float | None = None, holidays: set[str] | None = None) -> float:
        """Timestamp of the most recent 16:00 ET close (the start of 'overnight')."""
        now_dt = datetime.fromtimestamp(now or time.time(), ET)
        d = now_dt.date()
        for i in range(0, 10):
            dd = d - timedelta(days=i)
            if dd.weekday() >= 5 or dd.isoformat() in (holidays or set()):
                continue
            close = datetime(dd.year, dd.month, dd.day, 16, 0, tzinfo=ET)
            if close <= now_dt:
                return close.timestamp()
        return now_dt.timestamp() - 86400


def build_brief(
    alerts: list[dict[str, Any]],
    calendar: Calendar,
    snapshot: dict[str, dict[str, Any]],
    leads: list[float],
    now: float | None = None,
) -> dict[str, Any]:
    """The morning meeting: overnight catalysts, what's scheduled, what's moving, the edge."""
    now = now or time.time()
    since = Calendar.last_close(now, calendar.holidays)
    news = [a for a in alerts if a.get("kind") == "news" and a.get("created", 0) >= since]
    news.sort(key=lambda a: (a.get("analysis") or {}).get("score", 0), reverse=True)
    themes = Counter(t for a in news for t in (a.get("analysis") or {}).get("themes", []))
    indexes = {s: snapshot[s].get("chg_day") for s in ("SPY", "QQQ", "IWM", "DIA") if s in snapshot}
    movers = sorted(
        (
            {"symbol": s, "chg_day": v.get("chg_day"), "price": v.get("price")}
            for s, v in snapshot.items()
            if v.get("chg_day") is not None and s not in indexes
        ),
        key=lambda m: abs(m["chg_day"] or 0),
        reverse=True,
    )[:6]
    upcoming = calendar.upcoming(days=7, now=now)
    leads = sorted(leads)
    hour = datetime.fromtimestamp(now, ET).hour
    greeting = "Good morning" if 4 <= hour < 12 else "Good afternoon" if 12 <= hour < 18 else "Good evening"
    return {
        "greeting": greeting,
        "date": f"{datetime.fromtimestamp(now, ET):%A, %B} {datetime.fromtimestamp(now, ET).day}",
        "since": since,
        "market": calendar.market_status(now),
        "overnight": [
            {
                "id": a["id"],
                "title": a["title"],
                "severity": a["severity"],
                "created": a["created"],
                "tickers": ((a.get("edge") or {}).get("play") or {}).get("direct")
                or a.get("tickers", [])[:5],
                "direction": ((a.get("edge") or {}).get("play") or {}).get("direction")
                or (a.get("analysis") or {}).get("direction", ""),
                "source": (a.get("item") or {}).get("source", ""),
            }
            for a in news[:6]
        ],
        "count": len(news),
        "themes": [t.replace("_", " ") for t, _ in themes.most_common(4)],
        "indexes": indexes,
        "movers": movers,
        "calendar": upcoming[:6],
        "edge": {
            "stories": len(leads),
            "median_lead_s": leads[len(leads) // 2] if leads else None,
            "best_lead_s": leads[-1] if leads else None,
        },
    }


def brief_push(brief: dict[str, Any]) -> dict[str, Any] | None:
    """The 'morning meeting' notification, or None when there's nothing worth a ping."""
    lines = []
    for a in brief["overnight"][:2]:
        lines.append(
            ("▼ " if a["direction"] == "down" else "▲ " if a["direction"] == "up" else "• ") + a["title"]
        )
    today = [e for e in brief["calendar"] if e["in_days"] == 0 and e.get("impact", 1) >= 2]
    for e in today[:2]:
        lines.append(f"Today {e.get('time', '')} ET: {e['title']}".replace("  ", " "))
    if not lines:
        return None
    n = brief["count"]
    title = (
        f"☀️ The Brief · {n} overnight catalyst{'s' if n != 1 else ''}"
        if n
        else "☀️ The Brief · on the calendar today"
    )
    return {
        "id": "brief",
        "title": title,
        "body": "\n".join(lines)[:600],
        "severity": "HIGH",
        "kind": "brief",
        "tag": f"brief-{datetime.fromtimestamp(time.time(), ET).date().isoformat()}",
        "ts": time.time(),
        "url": "",
    }
