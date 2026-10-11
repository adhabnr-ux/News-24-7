"""The scorecard: after every big move, who knew first?

Axelrod's rule is that the edge is never assumed, it is audited. Every big move Foretape
alerts on (a price alert or an options alert that pushed) is checked against everything the
system saw about that company before it, and gets one verdict:

``pushed``    a news alert reached your phone before the move: the system was first.
``app``       the story was in the app before the move, but scored MEDIUM and didn't push.
``scored_low`` a headline about the company was read before the move and scored LOW. The
              source was there; a rule was missing. This is the cheapest gap to close.
``radar``     nothing before the move: the price was the first public trace, and the radar
              had it. The first headline that followed, and from where, is recorded.
``unexplained`` no headline within 6 hours either side: a source the system doesn't read.

Each gap comes with what to do about it, so the next move of that kind is caught earlier.
Everything is computed from the database (``items`` and ``alerts``), so the scorecard can be
rebuilt for any day still inside the retention window: ``news247 scorecard --date 2026-10-09``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .market.universe import COMMON_WORDS, name_key

ET = ZoneInfo("America/New_York")

VERDICTS = ("pushed", "app", "scored_low", "radar", "unexplained")
LABELS = {
    "pushed": "Pushed before the move",
    "app": "In the app before the move (not pushed)",
    "scored_low": "Headline read before the move, scored LOW",
    "radar": "Price moved first, the radar had it",
    "unexplained": "No headline within 6 hours",
}
AFTER_WINDOW_S = 6 * 3600  # how long after a move to look for the headline that explains it
LOOKBACK_S = 18 * 3600  # before a move: at least since the middle of the previous session
MOVE_KINDS = ("price", "options")
_SEV = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}


@dataclass
class Verdict:
    symbol: str
    at: float  # when Foretape alerted on the move
    kind: str  # price | options
    title: str
    alert_id: str
    verdict: str
    pct: float | None = None
    lead_s: float | None = None  # how long before the move the news was in hand
    lag_s: float | None = None  # radar: how long after the move the first headline came
    headline: dict[str, Any] = field(default_factory=dict)
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def fmt_span(s: float | None) -> str:
    if s is None:
        return "?"
    s = abs(s)
    if s < 90:
        return f"{s:.0f}s"
    if s < 5400:
        return f"{s / 60:.0f} min"
    if s < 2 * 86400:
        return f"{s / 3600:.1f} h"
    return f"{s / 86400:.1f} days"


def day_bounds(day: date) -> tuple[float, float]:
    """A trading day as the radar sees it: 04:00 to 20:00 ET (pre-market through after hours)."""
    start = datetime(day.year, day.month, day.day, 4, 0, tzinfo=ET).timestamp()
    end = datetime(day.year, day.month, day.day, 20, 0, tzinfo=ET).timestamp()
    return start, end


def _prev_close(ts: float, holidays: set[str]) -> float:
    d = datetime.fromtimestamp(ts, ET).date()
    for i in range(1, 10):
        dd = d - timedelta(days=i)
        if dd.weekday() < 5 and dd.isoformat() not in holidays:
            return datetime(dd.year, dd.month, dd.day, 16, 0, tzinfo=ET).timestamp()
    return ts - 86400


def _norm(title: str) -> str:
    """A headline in name_key's alphabet: "Crown Castle's" -> "crown castle", "AT&T" -> "at and t"."""
    t = title.lower().replace("&", " and ").replace("\u2019", "'")
    t = re.sub(r"'s\b", "", t)
    t = re.sub(r"[^\w\s-]", " ", t)
    return re.sub(r"\s+", " ", t)


def big_moves(alerts: list[dict[str, Any]], min_severity: str = "HIGH") -> list[dict[str, Any]]:
    """The first pushed price or options alert per company, oldest first."""
    floor = _SEV.get(min_severity, 2)
    seen: set[str] = set()
    out = []
    for a in sorted(alerts, key=lambda a: a.get("created", 0)):
        if a.get("kind") not in MOVE_KINDS or _SEV.get(str(a.get("severity")), 0) < floor:
            continue
        tickers = a.get("tickers") or []
        if len(tickers) != 1:  # a sector-wide multi-ticker move has no single "who knew"
            continue
        sym = tickers[0]
        if sym in seen:
            continue
        seen.add(sym)
        out.append(a)
    return out


def _move_pct(a: dict[str, Any]) -> float | None:
    mv = a.get("move") or {}
    pct = mv.get("pct", mv.get("change_pct"))
    if pct is None:
        radar = (a.get("edge") or {}).get("radar") or {}
        pct = radar.get("change_pct")
    try:
        return float(pct) if pct is not None else None
    except (TypeError, ValueError):
        return None


class Scorecard:
    """Builds verdicts from the database. ``universe`` (optional) lets a headline that names the
    company without its ticker ("Crown Castle jumps ...") count for CCI."""

    def __init__(self, storage: Any, universe: Any = None, holidays: set[str] | None = None) -> None:
        self.storage = storage
        self.universe = universe
        self.holidays = holidays or set()
        self._done: dict[str, dict[str, Any]] = {}  # finished days never change

    # ------------------------------------------------------------------ data access

    def _alerts(self, since: float, until: float) -> list[dict[str, Any]]:
        rows = self.storage.db.execute(
            "SELECT payload FROM alerts WHERE created >= ? AND created < ? ORDER BY created",
            (since, until),
        )
        return [json.loads(r[0]) for r in rows]

    def _items(self, since: float, until: float) -> list[dict[str, Any]]:
        rows = self.storage.db.execute(
            "SELECT uid, source, title, url, detected, tier, score, severity, tickers FROM items"
            " WHERE detected >= ? AND detected < ? ORDER BY detected",
            (since, until),
        )
        out = []
        for r in rows:
            d = dict(r)
            d["tickers"] = json.loads(d["tickers"] or "[]")
            out.append(d)
        return out

    def _name_pattern(self, wanted: set[str]) -> tuple[re.Pattern[str] | None, dict[str, str]]:
        """One regex for the wanted companies' names ("crown castle", "t-mobile us"), so each
        headline is matched once, however many companies moved. A one-word name only counts
        when it is distinctive (not ordinary headline vocabulary) and at least 5 letters."""
        if self.universe is None or not len(self.universe):
            return None, {}
        keys: dict[str, str] = {}
        for sym in wanted:
            li = self.universe.get(sym)
            key = name_key(li.name) if li is not None else ""
            words = key.split()
            if not words or (len(words) == 1 and (len(key) < 5 or key in COMMON_WORDS)):
                continue
            keys.setdefault(key, sym)
        if not keys:
            return None, {}
        alts = "|".join(re.escape(k) for k in sorted(keys, key=len, reverse=True))
        return re.compile(rf"(?<![\w-])(?:{alts})(?![\w-])"), keys

    def index(self, rows: list[dict[str, Any]], wanted: set[str]) -> dict[str, list[dict[str, Any]]]:
        """Rows (items or news alerts, oldest first) about each wanted company: its ticker is
        attached, or the headline names it."""
        pattern, keys = self._name_pattern(wanted)
        out: dict[str, list[dict[str, Any]]] = {s: [] for s in wanted}
        for r in rows:
            syms = wanted.intersection(r.get("tickers") or [])
            if pattern is not None:
                text = _norm(r.get("title", ""))
                syms.update(keys[m.group(0)] for m in pattern.finditer(text))
            for sym in syms:
                out[sym].append(r)
        return out

    # ------------------------------------------------------------------ verdicts

    def judge(
        self,
        move: dict[str, Any],
        news_alerts: list[dict[str, Any]],
        items: list[dict[str, Any]],
    ) -> Verdict:
        """``news_alerts`` and ``items``: what the system saw about this company (``index``)."""
        sym = move["tickers"][0]
        at = float(move["created"])
        since = min(at - LOOKBACK_S, _prev_close(at, self.holidays) - 6 * 3600)
        v = Verdict(
            symbol=sym,
            at=at,
            kind=move.get("kind", "price"),
            title=move.get("title", ""),
            alert_id=move.get("id", ""),
            verdict="unexplained",
            pct=_move_pct(move),
        )
        before = [a for a in news_alerts if since <= a.get("created", 0) < at]
        pushed = [a for a in before if _SEV.get(str(a.get("severity")), 0) >= 2]
        if pushed or before:
            first = (pushed or before)[0]
            v.verdict = "pushed" if pushed else "app"
            v.lead_s = at - first["created"]
            item = first.get("item") or {}
            v.headline = {
                "title": first.get("title", ""),
                "source": item.get("source", ""),
                "score": (first.get("analysis") or {}).get("score"),
                "severity": first.get("severity"),
                "at": first.get("created"),
            }
            if v.verdict == "app":
                v.hint = (
                    f"In the app {fmt_span(v.lead_s)} before the move at score "
                    f"{v.headline['score'] or '?'} (MEDIUM). A rule that weighs this kind of story "
                    "higher would have pushed it."
                )
            return v
        seen = [r for r in items if since <= r["detected"] < at]
        if seen:
            best = max(seen, key=lambda r: (r.get("score") or 0, -r["detected"]))
            first = seen[0]
            v.verdict = "scored_low"
            v.lead_s = at - first["detected"]
            v.headline = {
                "title": best["title"],
                "source": best["source"],
                "score": best.get("score"),
                "severity": best.get("severity"),
                "at": best["detected"],
                "url": best.get("url", ""),
            }
            v.hint = (
                f'Scorer gap: "{best["title"][:90]}" ({best["source"]}) was read '
                f"{fmt_span(at - best['detected'])} before the move and scored {best.get('score') or 0:.0f}. "
                f"Score it with `news247 score '{best['title'][:60]}'` and add the missing rule."
            )
            return v
        after = [r for r in items if at <= r["detected"] < at + AFTER_WINDOW_S]
        if after:
            first = after[0]
            v.verdict = "radar"
            v.lag_s = first["detected"] - at
            v.headline = {
                "title": first["title"],
                "source": first["source"],
                "score": first.get("score"),
                "severity": first.get("severity"),
                "at": first["detected"],
                "url": first.get("url", ""),
            }
            v.hint = (
                f"The price was first; the first headline came {fmt_span(v.lag_s)} later from "
                f"{first['source']}. If that is a re-write of a filing or a release, the original "
                "is where to look first next time."
            )
            return v
        v.hint = (
            "No headline about this company within 6 hours either side. It moved on something "
            "the system doesn't read: an X post, a broker note, a sector move or a filing type "
            "not on the list."
        )
        return v

    def build(self, day: date | str | None = None, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        if day is None:
            day = datetime.fromtimestamp(now, ET).date()
        elif isinstance(day, str):
            day = date.fromisoformat(day)
        key = day.isoformat()
        if key in self._done:
            return self._done[key]
        start, end = day_bounds(day)
        final = now >= end + AFTER_WINDOW_S  # every verdict's "after" window has closed
        end = min(end, now)
        moves = big_moves(self._alerts(start, end))
        verdicts: list[Verdict] = []
        if moves:
            look_from = min(
                min(m["created"] for m in moves) - LOOKBACK_S, _prev_close(start, self.holidays) - 6 * 3600
            )
            look_to = max(m["created"] for m in moves) + AFTER_WINDOW_S
            wanted = {m["tickers"][0] for m in moves}
            news = self.index(
                [a for a in self._alerts(look_from, look_to) if a.get("kind") == "news"], wanted
            )
            items = self.index(self._items(look_from, look_to), wanted)
            verdicts = [self.judge(m, news[m["tickers"][0]], items[m["tickers"][0]]) for m in moves]
        card = summarize(key, verdicts)
        if final:
            self._done[key] = card
            if len(self._done) > 60:
                self._done.pop(next(iter(self._done)))
        return card


def summarize(day: str, verdicts: list[Verdict]) -> dict[str, Any]:
    counts = {k: sum(v.verdict == k for v in verdicts) for k in VERDICTS}
    n = len(verdicts)
    ahead = counts["pushed"] + counts["app"]
    leads = sorted(v.lead_s for v in verdicts if v.verdict == "pushed" and v.lead_s is not None)
    lags = sorted(v.lag_s for v in verdicts if v.verdict == "radar" and v.lag_s is not None)
    order = {k: i for i, k in enumerate(("scored_low", "app", "unexplained", "radar", "pushed"))}
    return {
        "date": day,
        "moves": n,
        "counts": counts,
        "ahead": ahead,
        "ahead_rate": round(ahead / n, 3) if n else None,
        "pushed_rate": round(counts["pushed"] / n, 3) if n else None,
        "median_lead_s": leads[len(leads) // 2] if leads else None,
        "median_news_lag_s": lags[len(lags) // 2] if lags else None,
        # the gaps first: they are what to fix
        "verdicts": [v.to_dict() for v in sorted(verdicts, key=lambda v: (order[v.verdict], v.at))],
    }


def scorecard_line(card: dict[str, Any]) -> str:
    """One line for the Brief: 'Yesterday: 9 big moves · ahead on 4 (pushed 3, median 38 min early) ...'."""
    n = card.get("moves", 0)
    if not n:
        return ""
    c = card["counts"]
    parts = [f"{n} big move{'s' if n != 1 else ''}", f"ahead on {card['ahead']}"]
    if c["pushed"]:
        lead = f", median {fmt_span(card['median_lead_s'])} early" if card.get("median_lead_s") else ""
        parts[-1] += f" (pushed {c['pushed']}{lead})"
    if c["radar"]:
        parts.append(f"radar first on {c['radar']}")
    gaps = c["scored_low"] + c["app"]
    if gaps:
        parts.append(f"{gaps} scorer gap{'s' if gaps != 1 else ''}")
    if c["unexplained"]:
        parts.append(f"{c['unexplained']} unexplained")
    return " · ".join(parts)


def format_scorecard(card: dict[str, Any]) -> str:
    lines = [f"Scorecard for {card['date']}: who knew first?", ""]
    n = card["moves"]
    if not n:
        return "\n".join([*lines, "  No big moves (pushed price or options alerts) that day."])
    c = card["counts"]
    lines.append(f"  Big moves:                {n}")
    lead = (
        f"   (median {fmt_span(card['median_lead_s'])} before the move)" if card.get("median_lead_s") else ""
    )
    lines.append(f"  Pushed before the move:   {c['pushed']}/{n}{lead}")
    lines.append(f"  In the app before it:     {c['app']}/{n}")
    lines.append(f"  Read, but scored LOW:     {c['scored_low']}/{n}")
    lag = (
        f"   (first headline a median {fmt_span(card['median_news_lag_s'])} later)"
        if card.get("median_news_lag_s")
        else ""
    )
    lines.append(f"  Radar first:              {c['radar']}/{n}{lag}")
    lines.append(f"  Unexplained:              {c['unexplained']}/{n}")
    lines.append("")
    for v in card["verdicts"]:
        when = datetime.fromtimestamp(v["at"], ET).strftime("%H:%M")
        pct = f" {v['pct']:+.1f}%" if v.get("pct") is not None else ""
        lines.append(f"  {when} ET  {v['symbol']}{pct}  [{v['verdict']}]  {v['title'][:80]}")
        h = v.get("headline") or {}
        if h.get("title"):
            score = f", score {h['score']:.0f}" if isinstance(h.get("score"), (int, float)) else ""
            lines.append(f"      ↳ {h['title'][:100]} ({h.get('source', '')}{score})")
        if v.get("hint") and v["verdict"] != "pushed":
            lines.append(f"      → {v['hint']}")
    return "\n".join(lines)
