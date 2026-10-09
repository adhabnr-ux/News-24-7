"""Keeping the WhatsApp pairing across restarts on free hosts (temporary disk) via Postgres."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from pathlib import Path

import pytest

from news247.whatsapp.snapshot import PostgresSnapshot, sqlite_snapshot

PG = os.environ.get("NEWS247_TEST_PG", "")


def make_db(path: Path, rows: int) -> None:
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE IF NOT EXISTS whatsmeow_device (jid TEXT, key BLOB)")
    db.executemany(
        "INSERT INTO whatsmeow_device VALUES (?, ?)",
        [(f"1555000{i}@s.whatsapp.net", os.urandom(32)) for i in range(rows)],
    )
    db.commit()
    db.close()


def count(path: Path) -> int:
    with sqlite3.connect(path) as db:
        return db.execute("SELECT COUNT(*) FROM whatsmeow_device").fetchone()[0]


def test_snapshot_is_consistent_while_open(tmp_path):
    db_path = tmp_path / "s.db"
    make_db(db_path, 5)
    writer = sqlite3.connect(db_path)  # another process (the Go engine) has it open
    writer.execute("INSERT INTO whatsmeow_device VALUES ('x', x'00')")
    writer.commit()
    out = tmp_path / "copy.db"
    out.write_bytes(sqlite_snapshot(db_path))
    writer.close()
    assert count(out) == 6


@pytest.mark.skipif(not PG, reason="set NEWS247_TEST_PG=postgres://... to run against a real Postgres")
async def test_round_trip_through_postgres(tmp_path):
    db_path = tmp_path / "whatsapp" / "session.db"
    db_path.parent.mkdir()
    make_db(db_path, 3)
    snap = PostgresSnapshot(PG, db_path, key=f"test-{os.getpid()}")
    assert await snap.save() is True
    assert await snap.save() is False  # unchanged: no write (keeps free databases asleep)
    make_db(db_path, 1)
    assert await snap.save() is True and snap.saves == 2
    # a fresh container: the disk is empty, the pairing comes back
    db_path.unlink()
    fresh = PostgresSnapshot(PG, db_path, key=snap.key)
    assert await fresh.restore() is True and fresh.restored and count(db_path) == 4
    assert await fresh.save() is False  # restored == saved: nothing to upload
    # with a local session already present, nothing is overwritten
    assert await PostgresSnapshot(PG, db_path, key=snap.key).restore() is False
    # an unknown key restores nothing
    db_path.unlink()
    assert await PostgresSnapshot(PG, db_path, key="nobody").restore() is False


async def test_bad_database_is_reported_not_fatal(tmp_path):
    db_path = tmp_path / "session.db"
    make_db(db_path, 1)
    snap = PostgresSnapshot("postgres://u:p@127.0.0.1:9/x?sslmode=disable", db_path)
    assert await snap.save() is False and "save failed" in snap.last_error
    db_path.unlink()
    assert await snap.restore() is False and "restore failed" in snap.last_error
    with pytest.raises(ValueError):
        await asyncio.to_thread(snap._restore_sync.__func__, PostgresSnapshot("mysql://x", db_path))  # type: ignore[attr-defined]


async def test_run_loop_saves_soon_after_pairing(tmp_path, monkeypatch):
    db_path = tmp_path / "session.db"
    make_db(db_path, 1)
    snap = PostgresSnapshot("postgres://unused", db_path, interval_s=3600)
    saved: list[bool] = []

    async def fake_save(force: bool = False) -> bool:
        saved.append(True)
        return True

    monkeypatch.setattr(snap, "save", fake_save)
    snap.settle_s = 0
    stop, soon = asyncio.Event(), asyncio.Event()
    task = asyncio.ensure_future(snap.run(stop, soon))
    soon.set()  # e.g. just paired
    for _ in range(100):
        if saved:
            break
        await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert saved == [True]
