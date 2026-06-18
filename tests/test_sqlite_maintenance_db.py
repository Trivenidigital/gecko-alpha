"""DB maintenance methods (P0 Part B): WAL checkpoint + incremental vacuum."""

import aiosqlite

from scout.db import Database


async def _wal_db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    return db


async def test_checkpoint_wal_truncate_returns_tuple(tmp_path):
    db = await _wal_db(tmp_path)
    await db._conn.execute("CREATE TABLE t(x)")
    await db._conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(500)])
    await db._conn.commit()
    res = await db.checkpoint_wal_truncate()
    assert set(res) == {"busy", "log_frames", "checkpointed_frames"}
    assert res["busy"] == 0  # sole connection → not busy
    assert res["checkpointed_frames"] >= 0
    await db.close()


async def test_checkpoint_busy_with_concurrent_reader(tmp_path):
    """Fold 6: a second connection holding an open read transaction pins the
    WAL so checkpoint(TRUNCATE) cannot truncate → busy == 1."""
    db = await _wal_db(tmp_path)
    await db._conn.execute("CREATE TABLE t(x)")
    await db._conn.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(300)])
    await db._conn.commit()

    reader = await aiosqlite.connect(str(tmp_path / "t.db"))
    try:
        await reader.execute("BEGIN")
        await (await reader.execute("SELECT COUNT(*) FROM t")).fetchall()  # pin snapshot
        await db._conn.execute("INSERT INTO t VALUES (999)")
        await db._conn.commit()
        res = await db.checkpoint_wal_truncate()
        assert res["busy"] == 1
    finally:
        await reader.rollback()
        await reader.close()
        await db.close()


async def _incremental_db(tmp_path, n=20000):
    db = Database(str(tmp_path / "iv.db"))
    await db.initialize()
    await db._conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
    await db._conn.execute("VACUUM")  # apply the mode on the populated db
    await db._conn.execute("CREATE TABLE big(x)")
    await db._conn.executemany("INSERT INTO big VALUES (?)", [(i,) for i in range(n)])
    await db._conn.commit()
    await db._conn.execute("DELETE FROM big")
    await db._conn.commit()
    return db


async def _freelist(db) -> int:
    return (await (await db._conn.execute("PRAGMA freelist_count")).fetchone())[0]


async def test_incremental_vacuum_reclaims_all(tmp_path):
    """Fold 1: fetchall() drives the pragma to drain the whole freelist."""
    db = await _incremental_db(tmp_path)
    before = await _freelist(db)
    assert before > 1  # proves multi-page drain, not the 1-page execute() bug
    res = await db.run_incremental_vacuum(max_pages=0)
    assert res["auto_vacuum"] == 2
    assert res["freelist_before"] == before
    assert res["freelist_after"] == 0
    assert res["pages_reclaimed"] == before
    await db.close()


async def test_incremental_vacuum_caps_at_max_pages(tmp_path):
    """Fold 1: incremental_vacuum(N) caps reclamation at exactly N pages."""
    db = await _incremental_db(tmp_path)
    before = await _freelist(db)
    assert before >= 4
    cap = before // 2
    res = await db.run_incremental_vacuum(max_pages=cap)
    assert res["pages_reclaimed"] == cap
    assert res["freelist_after"] == before - cap
    await db.close()


async def test_incremental_vacuum_noop_when_auto_vacuum_none(tmp_path):
    db = await _wal_db(tmp_path)  # default auto_vacuum=0 (NONE)
    res = await db.run_incremental_vacuum()
    assert res["auto_vacuum"] == 0
    assert res["pages_reclaimed"] == 0
    await db.close()
