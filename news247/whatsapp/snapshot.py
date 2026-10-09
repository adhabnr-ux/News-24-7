"""Keep the WhatsApp pairing alive on hosts with a temporary disk (Render Free, Koyeb...).

The linked-device engine stores its keys in a local SQLite file. On free hosts that file is wiped
whenever the service restarts or redeploys, which would mean scanning the QR code again. This
module copies the file into a free hosted Postgres (Neon, Supabase...) and puts it back on start:

* a consistent copy is taken with SQLite's online-backup API (safe while the engine writes),
  gzipped, and uploaded **only when it changed**, at most every ``interval_s`` and right after
  pairing, plus once on shutdown (Render sends SIGTERM before a restart);
* so the database is touched a few times an hour at most, which fits free plans that pause
  idle databases and meter compute time (Neon Free: 100 compute-hours a month).

One table, one row: ``news247_state(key, value, sha256, updated)``.
"""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import hashlib
import logging
import sqlite3
import ssl
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

log = logging.getLogger(__name__)

TABLE = "news247_state"


def _connect(url: str) -> Any:
    u = urlsplit(url)
    if u.scheme not in ("postgres", "postgresql"):
        raise ValueError("WHATSAPP_STATE_DB must be a postgres:// or postgresql:// URL")
    try:
        import pg8000.native
    except ImportError:
        raise RuntimeError("the pairing backup needs pg8000: pip install 'news247[whatsapp]'") from None
    query = {k: v[-1] for k, v in parse_qs(u.query).items()}
    sslmode = query.get("sslmode", "require" if u.hostname not in ("localhost", "127.0.0.1") else "disable")
    ctx = None
    if sslmode != "disable":
        ctx = ssl.create_default_context()
        if sslmode in ("require", "prefer", "allow"):  # encrypted, like libpq's "require"
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    return pg8000.native.Connection(
        user=unquote(u.username or ""),
        password=unquote(u.password or "") or None,
        host=u.hostname or "localhost",
        port=u.port or 5432,
        database=(u.path or "/").lstrip("/") or None,
        ssl_context=ctx,
        timeout=30,
        application_name="news247",
    )


def sqlite_snapshot(path: Path) -> bytes:
    """A consistent copy of a live SQLite database, as bytes."""
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "copy.db"
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
        dst = sqlite3.connect(copy)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        return copy.read_bytes()


class PostgresSnapshot:
    def __init__(
        self, url: str, db_path: Path, key: str = "whatsapp-session", interval_s: float = 1800
    ) -> None:
        self.url = url
        self.db_path = db_path
        self.key = key
        self.interval_s = interval_s
        self.last_sha = ""
        self.last_saved: float | None = None
        self.last_error = ""
        self.restored = False
        self.saves = 0
        self.settle_s = 20.0

    # sync helpers run in a thread (pg8000 is synchronous; this happens a few times an hour)

    def _ensure_table(self, conn: Any) -> None:
        conn.run(
            f"CREATE TABLE IF NOT EXISTS {TABLE} (key TEXT PRIMARY KEY, value BYTEA NOT NULL,"
            " sha256 TEXT NOT NULL, updated TIMESTAMPTZ NOT NULL DEFAULT now())"
        )

    def _restore_sync(self) -> bool:
        conn = _connect(self.url)
        try:
            self._ensure_table(conn)
            rows = conn.run(f"SELECT value, sha256 FROM {TABLE} WHERE key = :k", k=self.key)
        finally:
            conn.close()
        if not rows:
            return False
        blob, sha = rows[0]
        data = gzip.decompress(bytes(blob))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.db_path.with_suffix(".restore")
        tmp.write_bytes(data)
        for extra in ("-wal", "-shm", "-journal"):  # stale journals must not be replayed onto it
            with contextlib.suppress(FileNotFoundError):
                Path(str(self.db_path) + extra).unlink()
        tmp.replace(self.db_path)
        self.last_sha = sha
        return True

    def _save_sync(self, force: bool = False) -> bool:
        if not self.db_path.is_file():
            return False
        raw = sqlite_snapshot(self.db_path)
        sha = hashlib.sha256(raw).hexdigest()
        if sha == self.last_sha and not force:
            return False
        blob = gzip.compress(raw, compresslevel=6)
        conn = _connect(self.url)
        try:
            self._ensure_table(conn)
            conn.run(
                f"INSERT INTO {TABLE} (key, value, sha256, updated) VALUES (:k, :v, :s, now())"
                " ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, sha256 = EXCLUDED.sha256,"
                " updated = now()",
                k=self.key,
                v=blob,
                s=sha,
            )
        finally:
            conn.close()
        self.last_sha, self.last_saved = sha, time.time()
        self.saves += 1
        return True

    # async API

    async def restore(self) -> bool:
        """Put the saved session back if there's no local one (fresh container)."""
        if self.db_path.is_file():
            self.restored = False
            return False
        try:
            self.restored = await asyncio.to_thread(self._restore_sync)
            self.last_error = ""
            if self.restored:
                log.info("WhatsApp pairing restored from the state database")
            return self.restored
        except Exception as exc:  # noqa: BLE001 - without it we just pair again
            self.last_error = f"restore failed: {exc}"[:300]
            log.warning("WhatsApp state database: %s", self.last_error)
            return False

    async def save(self, force: bool = False) -> bool:
        try:
            saved = await asyncio.to_thread(self._save_sync, force)
            self.last_error = ""
            if saved:
                log.info("WhatsApp pairing saved to the state database")
            return saved
        except Exception as exc:  # noqa: BLE001 - try again next round
            self.last_error = f"save failed: {exc}"[:300]
            log.warning("WhatsApp state database: %s", self.last_error)
            return False

    async def run(self, stop: asyncio.Event, soon: asyncio.Event) -> None:
        """Save every ``interval_s`` if changed, or shortly after ``soon`` is set (e.g. pairing)."""
        while not stop.is_set():
            waiter = asyncio.ensure_future(soon.wait())
            stopper = asyncio.ensure_future(stop.wait())
            try:
                await asyncio.wait(
                    {waiter, stopper}, timeout=self.interval_s, return_when=asyncio.FIRST_COMPLETED
                )
            finally:
                waiter.cancel()
                stopper.cancel()
            if stop.is_set():
                break
            if soon.is_set():
                soon.clear()
                await asyncio.sleep(self.settle_s)  # let the engine finish writing the new keys
            await self.save()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "restored": self.restored,
            "last_saved": self.last_saved,
            "saves": self.saves,
            "last_error": self.last_error,
        }
