"""W3 — `_persist_signal_row` transaction discipline (binding criterion 1).

The pre-PR1 writer executed INSERT + commit on the shared connection with no
lock at all. Every other writer on that connection holds ``db._txn_lock``;
one that does not can commit another writer's in-flight statements out from
under it. PR1 touches this writer, so PR1 fixes it.

The lock-placement test drives the REAL ``asyncio.Lock``. Mocking the lock
would assert that we called something named "lock", which is not the property
under test — the property is that no commit lands while another holder has it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import aiosqlite
import pytest

from scout.db import Database
from scout.social.telegram.listener import _persist_signal_row


async def _message_row(db: Database) -> int:
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = await db._conn.execute(
        "INSERT INTO tg_social_messages "
        "(channel_handle, msg_id, posted_at, parsed_at) VALUES ('@gem', 1, ?, ?)",
        (now_iso, now_iso),
    )
    await db._conn.commit()
    return cur.lastrowid


async def _persist(db: Database, message_pk: int, **overrides) -> int | None:
    kwargs = dict(
        db=db,
        message_pk=message_pk,
        token_id="tok",
        symbol="TOK",
        contract_address="0xabc",
        chain="solana",
        mcap=250_000.0,
        resolution_state="RESOLVED",
        channel_handle="@gem",
        paper_trade_id=None,
        resolution_snapshot_json=None,
    )
    kwargs.update(overrides)
    return await _persist_signal_row(**kwargs)


@pytest.mark.asyncio
async def test_returns_signal_id_of_inserted_row(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        message_pk = await _message_row(db)
        signal_id = await _persist(db, message_pk)
        assert signal_id is not None
        cur = await db._conn.execute("SELECT id FROM tg_social_signals")
        assert [r[0] for r in await cur.fetchall()] == [signal_id]
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_persists_the_snapshot_column(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        message_pk = await _message_row(db)
        payload = json.dumps({"snapshot_schema_version": 1, "price_usd": 1.5})
        signal_id = await _persist(db, message_pk, resolution_snapshot_json=payload)
        cur = await db._conn.execute(
            "SELECT resolution_snapshot_json FROM tg_social_signals WHERE id = ?",
            (signal_id,),
        )
        (stored,) = await cur.fetchone()
        assert stored == payload
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_writer_does_not_commit_while_txn_lock_is_held(tmp_path):
    """Hold `db._txn_lock` externally; the writer must block BEFORE its INSERT
    and commit, and complete only once the lock is released."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        message_pk = await _message_row(db)

        await db._txn_lock.acquire()
        task = asyncio.create_task(_persist(db, message_pk))

        # Yield generously: if the writer were lock-free it would have run to
        # completion by now, and the row would be visible.
        for _ in range(20):
            await asyncio.sleep(0)
        assert not task.done(), "writer completed while _txn_lock was held"

        cur = await db._conn.execute("SELECT COUNT(*) FROM tg_social_signals")
        (count_while_blocked,) = await cur.fetchone()
        assert count_while_blocked == 0, "writer committed under a held lock"

        db._txn_lock.release()
        signal_id = await asyncio.wait_for(task, timeout=5)
        assert signal_id is not None

        cur = await db._conn.execute("SELECT COUNT(*) FROM tg_social_signals")
        (count_after,) = await cur.fetchone()
        assert count_after == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_failed_insert_rolls_back_and_releases_the_lock(tmp_path):
    """A NOT NULL violation propagates loudly, leaves no row, and does not
    strand `_txn_lock` — a stranded lock deadlocks every later writer."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        message_pk = await _message_row(db)

        with pytest.raises(aiosqlite.IntegrityError):
            await _persist(db, message_pk, token_id=None)

        assert not db._txn_lock.locked(), "_txn_lock stranded after a failed write"

        cur = await db._conn.execute("SELECT COUNT(*) FROM tg_social_signals")
        (count,) = await cur.fetchone()
        assert count == 0

        # The connection is usable again: the rollback cleared the aborted txn.
        signal_id = await _persist(db, message_pk)
        assert signal_id is not None
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_concurrent_writers_serialize(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    try:
        message_pk = await _message_row(db)
        ids = await asyncio.gather(
            _persist(db, message_pk, token_id="a"),
            _persist(db, message_pk, token_id="b"),
            _persist(db, message_pk, token_id="c"),
        )
        assert len(set(ids)) == 3
        cur = await db._conn.execute("SELECT COUNT(*) FROM tg_social_signals")
        (count,) = await cur.fetchone()
        assert count == 3
    finally:
        await db.close()
