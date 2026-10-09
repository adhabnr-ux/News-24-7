"""Small durable blobs (push subscriptions, settings) for hosts whose disk is temporary.

On Render Free the disk is wiped on every restart and deploy. With ``STATE_DB`` set to a free
Postgres URL (Neon, Supabase…), News247 keeps a copy of what must survive there; otherwise it
only uses the local SQLite database. Writes happen only when something changes (a phone
subscribes or unsubscribes), so free plans that pause idle databases are fine.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)


class StateStore:
    def __init__(self, url: str) -> None:
        self.url = url
        self.last_error = ""

    def _run(self, sql: str, **params: Any) -> list[Any]:
        from .whatsapp.snapshot import TABLE, _connect

        conn = _connect(self.url)
        try:
            conn.run(
                f"CREATE TABLE IF NOT EXISTS {TABLE} (key TEXT PRIMARY KEY, value BYTEA NOT NULL,"
                " sha256 TEXT NOT NULL, updated TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
            return conn.run(sql.replace("{TABLE}", TABLE), **params) or []
        finally:
            conn.close()

    async def get(self, key: str) -> bytes | None:
        try:
            rows = await asyncio.to_thread(self._run, "SELECT value FROM {TABLE} WHERE key = :k", k=key)
            self.last_error = ""
            return bytes(rows[0][0]) if rows else None
        except Exception as exc:  # noqa: BLE001 - fall back to local state
            self.last_error = f"read failed: {exc}"[:300]
            log.warning("state database: %s", self.last_error)
            return None

    async def put(self, key: str, value: bytes) -> bool:
        import hashlib

        try:
            await asyncio.to_thread(
                self._run,
                "INSERT INTO {TABLE} (key, value, sha256, updated) VALUES (:k, :v, :s, now())"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, sha256 = EXCLUDED.sha256, updated = now()",
                k=key,
                v=value,
                s=hashlib.sha256(value).hexdigest(),
            )
            self.last_error = ""
            return True
        except Exception as exc:  # noqa: BLE001 - kept locally, retried on the next change
            self.last_error = f"write failed: {exc}"[:300]
            log.warning("state database: %s", self.last_error)
            return False
