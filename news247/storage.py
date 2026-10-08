"""SQLite persistence: seen items (so restarts never re-alert), alert history, latency stats."""

from __future__ import annotations

import json
import sqlite3
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any

from .models import Alert, Analysis, NewsItem

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    uid TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT,
    summary TEXT,
    published REAL,
    detected REAL NOT NULL,
    tier TEXT,
    score REAL,
    severity TEXT,
    tickers TEXT,
    themes TEXT,
    direction TEXT,
    llm_summary TEXT,
    story_id INTEGER,
    alerted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_detected ON items(detected);
CREATE INDEX IF NOT EXISTS idx_items_score ON items(score);
CREATE TABLE IF NOT EXISTS alerts (
    id TEXT PRIMARY KEY,
    created REAL NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    url TEXT,
    tickers TEXT,
    payload TEXT
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created);
"""


class Storage:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._seen_max = 200_000
        self._load_seen()

    def _load_seen(self, days: float = 7) -> None:
        cutoff = time.time() - days * 86400
        for (uid,) in self.db.execute(
            "SELECT uid FROM items WHERE detected >= ? ORDER BY detected", (cutoff,)
        ):
            self._seen[uid] = None

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ items

    def seen(self, uid: str) -> bool:
        return uid in self._seen

    def mark_seen(self, uid: str) -> None:
        self._seen[uid] = None
        if len(self._seen) > self._seen_max:
            self._seen.popitem(last=False)

    def add_item(self, item: NewsItem, analysis: Analysis, story_id: int | None = None) -> None:
        self.mark_seen(item.uid)
        self.db.execute(
            """INSERT OR IGNORE INTO items (uid, source, title, url, summary, published, detected, tier, score,
               severity, tickers, themes, direction, llm_summary, story_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                item.uid,
                item.source,
                item.title,
                item.url,
                item.summary,
                item.published,
                item.detected,
                item.tier.name,
                analysis.score,
                analysis.severity.name,
                json.dumps(analysis.tickers),
                json.dumps(analysis.themes),
                analysis.direction,
                analysis.summary,
                story_id,
            ),
        )

    def update_analysis(self, uid: str, analysis: Analysis) -> None:
        self.db.execute(
            "UPDATE items SET score=?, severity=?, tickers=?, themes=?, direction=?, llm_summary=? WHERE uid=?",
            (
                analysis.score,
                analysis.severity.name,
                json.dumps(analysis.tickers),
                json.dumps(analysis.themes),
                analysis.direction,
                analysis.summary,
                uid,
            ),
        )

    def mark_alerted(self, uid: str) -> None:
        self.db.execute("UPDATE items SET alerted=1 WHERE uid=?", (uid,))

    def recent_items(
        self, limit: int = 200, min_score: float = 0.0, since: float | None = None
    ) -> list[dict[str, Any]]:
        q = "SELECT * FROM items WHERE score >= ?"
        args: list[Any] = [min_score]
        if since:
            q += " AND detected >= ?"
            args.append(since)
        q += " ORDER BY detected DESC LIMIT ?"
        args.append(limit)
        return [self._item_row(r) for r in self.db.execute(q, args)]

    @staticmethod
    def _item_row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d["tickers"] = json.loads(d["tickers"] or "[]")
        d["themes"] = json.loads(d["themes"] or "[]")
        return d

    # ------------------------------------------------------------------ alerts

    def add_alert(self, alert: Alert) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO alerts (id, created, kind, severity, title, body, url, tickers, payload)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (
                alert.id,
                alert.created,
                alert.kind,
                alert.severity.name,
                alert.title,
                alert.body,
                alert.url,
                json.dumps(alert.tickers),
                json.dumps(alert.to_dict(), default=str),
            ),
        )

    def recent_alerts(self, limit: int = 100, since: float | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT payload FROM alerts", []
        if since:
            q += " WHERE created >= ?"
            args.append(since)
        q += " ORDER BY created DESC LIMIT ?"
        args.append(limit)
        return [json.loads(r[0]) for r in self.db.execute(q, args)]

    # ------------------------------------------------------------------ stats

    def latency_stats(self, since: float | None = None) -> list[dict[str, Any]]:
        """Per-source median/p90 publish->detect lag, for items with a publish time."""
        since = since or time.time() - 7 * 86400
        rows = self.db.execute(
            "SELECT source, detected - published AS lag FROM items WHERE published IS NOT NULL AND detected >= ?"
            " AND detected - published >= 0 AND detected - published < 86400 ORDER BY source",
            (since,),
        ).fetchall()
        by: dict[str, list[float]] = {}
        for r in rows:
            by.setdefault(r["source"], []).append(r["lag"])
        out = []
        for src, lags in sorted(by.items()):
            lags.sort()
            out.append(
                {
                    "source": src,
                    "items": len(lags),
                    "median_s": lags[len(lags) // 2],
                    "p90_s": lags[min(len(lags) - 1, int(len(lags) * 0.9))],
                }
            )
        return out

    def counts(self) -> dict[str, Any]:
        day = time.time() - 86400
        c = self.db.execute
        return {
            "items_total": c("SELECT COUNT(*) FROM items").fetchone()[0],
            "items_24h": c("SELECT COUNT(*) FROM items WHERE detected >= ?", (day,)).fetchone()[0],
            "alerts_24h": c("SELECT COUNT(*) FROM alerts WHERE created >= ?", (day,)).fetchone()[0],
        }

    def prune(self, retention_days: int) -> int:
        cutoff = time.time() - retention_days * 86400
        n = self.db.execute("DELETE FROM items WHERE detected < ?", (cutoff,)).rowcount
        self.db.execute("DELETE FROM alerts WHERE created < ?", (cutoff,))
        return n
