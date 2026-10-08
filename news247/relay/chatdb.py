"""Read-only access to the Messages database (~/Library/Messages/chat.db) on the relay Mac.

AppleScript's ``send`` returns as soon as Messages *accepts* a message, so on its own it can't
tell whether the text reached your phone. The database can: each outgoing row gets ``error``
(non-zero = failed, the red "Not Delivered") and ``is_delivered`` / ``date_delivered`` (your
iPhone acknowledged it). The same database lets you text the relay commands like "pause 2h".

Reading it needs Full Disk Access for the relay's Python (the installer explains how). Without
it the relay still sends; it just can't confirm delivery or take commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path.home() / "Library" / "Messages" / "chat.db"
APPLE_EPOCH = 978307200  # 2001-01-01T00:00:00Z in unix seconds


def apple_to_unix(value: int | float | None) -> float | None:
    """Messages stores nanoseconds since 2001 (macOS 10.13+); older systems used seconds."""
    if not value:
        return None
    v = float(value)
    return (v / 1e9 if v > 1e11 else v) + APPLE_EPOCH


def unix_to_apple(ts: float) -> int:
    return int((ts - APPLE_EPOCH) * 1e9)


def decode_attributed_body(blob: bytes | None) -> str:
    """Extract the plain text from ``message.attributedBody`` (an archived NSAttributedString).

    Since macOS 13 the ``text`` column is often empty and the words only live in this blob. The
    string follows the first "NSString" class name: a few header bytes, then a length (one byte,
    or 0x81 + 2 bytes little-endian, or 0x82 + 4 bytes), then UTF-8.
    """
    if not blob:
        return ""
    idx = blob.find(b"NSString")
    if idx < 0:
        return ""
    rest = blob[idx + len(b"NSString") :]
    plus = rest.find(b"+")  # the type marker that precedes the length
    if plus < 0 or plus > 8:
        return ""
    rest = rest[plus + 1 :]
    if not rest:
        return ""
    lead = rest[0]
    if lead == 0x81:
        length, start = int.from_bytes(rest[1:3], "little"), 3
    elif lead == 0x82:
        length, start = int.from_bytes(rest[1:5], "little"), 5
    else:
        length, start = lead, 1
    return rest[start : start + length].decode("utf-8", errors="replace")


def _handle_variants(handle: str) -> list[str]:
    """chat.db stores phone handles as +15551234567, sometimes without '+'; e-mails lowercase."""
    h = handle.strip()
    if "@" in h:
        return [h.lower(), h]
    digits = re.sub(r"\D", "", h)
    if len(digits) == 10:  # US number without the country code
        digits = "1" + digits
    out = {h, "+" + digits, digits}
    if len(digits) == 11 and digits.startswith("1"):
        out.add(digits[1:])
    return sorted(out)


class ChatDB:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_PATH
        self.last_error = ""

    def _connect(self) -> sqlite3.Connection:
        # read-only and never creates the file; Messages keeps writing to it (WAL), we only read
        db = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, timeout=2)
        db.row_factory = sqlite3.Row
        return db

    def available(self) -> bool:
        try:
            with contextlib.closing(self._connect()) as db:
                db.execute("SELECT ROWID FROM message LIMIT 1").fetchall()
            self.last_error = ""
            return True
        except sqlite3.Error as exc:
            msg = str(exc)
            self.last_error = (
                "no access to the Messages database: give the relay Full Disk Access "
                "(System Settings > Privacy & Security > Full Disk Access)"
                if "unable to open" in msg or "authorization" in msg or "not permitted" in msg
                else msg
            )
            return False

    # ------------------------------------------------------------------ delivery receipts

    def outgoing_status(self, handle: str, since: float) -> dict[str, Any] | None:
        """Newest message sent to ``handle`` at/after unix time ``since`` (None if not there yet)."""
        variants = _handle_variants(handle)
        marks = ",".join("?" * len(variants))
        with contextlib.closing(self._connect()) as db:
            row = db.execute(
                f"""SELECT m.ROWID AS rowid, m.error, m.is_sent, m.is_delivered, m.date, m.date_delivered,
                           m.service
                    FROM message m JOIN handle h ON m.handle_id = h.ROWID
                    WHERE m.is_from_me = 1 AND h.id IN ({marks}) AND m.date >= ?
                    ORDER BY m.date DESC LIMIT 1""",
                (*variants, unix_to_apple(since - 2)),
            ).fetchone()
        if row is None:
            return None
        sent = apple_to_unix(row["date"])
        delivered = apple_to_unix(row["date_delivered"])
        return {
            "rowid": row["rowid"],
            "error": int(row["error"] or 0),
            "is_sent": bool(row["is_sent"]),
            "is_delivered": bool(row["is_delivered"]),
            "service": row["service"] or "",
            "sent_at": sent,
            "delivered_at": delivered if row["is_delivered"] else None,
        }

    async def wait_receipt(
        self, handle: str, since: float, timeout: float = 20.0, poll: float = 0.5
    ) -> dict[str, Any]:
        """Poll until the message is delivered or failed, or ``timeout`` passes.

        Returns {"status": "delivered" | "failed" | "sent" | "unknown", ...}. "sent" means Apple
        accepted it but your phone hasn't confirmed yet (e.g. it's off or has no signal).
        """
        deadline = time.monotonic() + timeout
        last: dict[str, Any] | None = None
        while True:
            try:
                last = await asyncio.to_thread(self.outgoing_status, handle, since)
            except sqlite3.Error as exc:
                return {"status": "unknown", "error": str(exc)}
            if last is not None:
                if last["error"]:
                    return {"status": "failed", "error_code": last["error"], **last}
                if last["is_delivered"]:
                    return {"status": "delivered", **last}
            if time.monotonic() >= deadline:
                if last is None:
                    return {"status": "unknown", "error": "message not found in the Messages database"}
                return {"status": "sent", **last}
            await asyncio.sleep(poll)

    # ------------------------------------------------------------------ inbound texts

    def max_rowid(self) -> int:
        with contextlib.closing(self._connect()) as db:
            return int(db.execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()[0])

    def incoming_after(self, rowid: int, handles: list[str]) -> list[dict[str, Any]]:
        """Messages *received* from any of ``handles`` with ROWID > rowid, oldest first."""
        variants = sorted({v for h in handles for v in _handle_variants(h)})
        if not variants:
            return []
        marks = ",".join("?" * len(variants))
        with contextlib.closing(self._connect()) as db:
            rows = db.execute(
                f"""SELECT m.ROWID AS rowid, m.guid, m.text, m.attributedBody, m.date, h.id AS handle
                    FROM message m JOIN handle h ON m.handle_id = h.ROWID
                    WHERE m.is_from_me = 0 AND m.ROWID > ? AND h.id IN ({marks})
                    ORDER BY m.ROWID LIMIT 50""",
                (rowid, *variants),
            ).fetchall()
        out = []
        for r in rows:
            text = r["text"] or decode_attributed_body(r["attributedBody"])
            out.append(
                {
                    "rowid": r["rowid"],
                    "guid": r["guid"],
                    "from": r["handle"],
                    "text": text.replace("￼", "").strip(),
                    "at": apple_to_unix(r["date"]),
                }
            )
        return out

    def own_handles(self) -> list[str]:
        """The Apple ID handles this Mac sends from (e.g. ["e:bot@icloud.com"]), best effort."""
        with contextlib.closing(self._connect()) as db:
            try:
                rows = db.execute(
                    "SELECT account FROM message WHERE is_from_me = 1 AND account IS NOT NULL"
                    " ORDER BY ROWID DESC LIMIT 200"
                ).fetchall()
            except sqlite3.Error:
                return []
        out: list[str] = []
        for (acct,) in rows:
            handle = acct.split(":", 1)[1] if acct and ":" in acct else ""
            if handle and handle not in out:
                out.append(handle)
        return out
