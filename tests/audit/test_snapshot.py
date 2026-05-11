import pytest
import aiosqlite
from scout.db import Database


@pytest.mark.asyncio
async def test_migration_creates_audit_volume_snapshot_table(tmp_path):
    """Schema migration creates audit_volume_snapshot_phase_b with correct columns + UNIQUE constraint."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    # Verify table exists with expected columns
    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute("PRAGMA table_info(audit_volume_snapshot_phase_b)")
        cols = {row[1]: row[2] for row in await cur.fetchall()}
    assert "coin_id" in cols
    assert "symbol" in cols
    assert "name" in cols
    assert "volume_24h" in cols
    assert "market_cap" in cols
    assert "price" in cols
    assert "recorded_at" in cols
    assert "snapshotted_at" in cols

    # Verify schema_version row
    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT description FROM schema_version WHERE version = ?", (20260518,)
        )
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == "bl_audit_volume_snapshot_phase_b"

    await db.close()


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path):
    """Running migration twice (simulating pipeline restart) does not error or duplicate schema_version row."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()
    await db.close()

    # Simulate pipeline restart: open a new Database against the same file.
    # The migration must be a no-op on second invocation (CREATE TABLE IF NOT EXISTS
    # + INSERT OR IGNORE on schema_version).
    db2 = Database(str(db_path))
    await db2.connect()

    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM schema_version WHERE version = ?", (20260518,)
        )
        count = (await cur.fetchone())[0]
    assert count == 1

    await db2.close()


@pytest.mark.asyncio
async def test_unique_constraint_prevents_duplicate(tmp_path):
    """UNIQUE (coin_id, recorded_at) prevents duplicate rows."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    now_iso = "2026-05-11T12:00:00+00:00"
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "INSERT INTO audit_volume_snapshot_phase_b "
            "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at, snapshotted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("test-coin", "TEST", "Test", 1000.0, 5000.0, 0.5, now_iso, now_iso),
        )
        await conn.commit()
        # Second insert with same (coin_id, recorded_at) must fail
        with pytest.raises(aiosqlite.IntegrityError):
            await conn.execute(
                "INSERT INTO audit_volume_snapshot_phase_b "
                "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at, snapshotted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("test-coin", "TEST", "Test", 2000.0, 5500.0, 0.6, now_iso, now_iso),
            )
            await conn.commit()

    await db.close()
