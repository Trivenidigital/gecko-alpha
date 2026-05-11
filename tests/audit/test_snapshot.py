import pytest
import aiosqlite
from scout.db import Database
from scout.audit.snapshot import snapshot_volume_history_for_phase_b


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


@pytest.mark.asyncio
async def test_snapshot_empty_cohort(tmp_path):
    """Snapshot with no slow_burn detections returns (0, 0)."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    rows, coin_ids = await snapshot_volume_history_for_phase_b(
        db,
        soak_start_iso="2026-05-10T00:00:00+00:00",
        soak_end_iso="2026-05-25T00:00:00+00:00",
    )
    assert rows == 0
    assert coin_ids == 0
    await db.close()


@pytest.mark.asyncio
async def test_snapshot_basic_capture(tmp_path):
    """Snapshot captures volume_history_cg rows for slow_burn-detected coin_ids."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    # Seed: one slow_burn detection + 3 volume_history_cg rows for it
    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "INSERT INTO slow_burn_candidates "
            "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("coin-a", "AAA", "AlphaCoin", 75.0, -1.5, "2026-05-10T03:50:00+00:00"),
        )
        for i, ts in enumerate([
            "2026-05-10T04:00:00+00:00",
            "2026-05-10T05:00:00+00:00",
            "2026-05-10T06:00:00+00:00",
        ]):
            await conn.execute(
                "INSERT INTO volume_history_cg "
                "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("coin-a", "AAA", "AlphaCoin", 100000.0 + i, 5e6, 0.1 + i * 0.01, ts),
            )
        await conn.commit()

    rows, coin_ids = await snapshot_volume_history_for_phase_b(
        db,
        soak_start_iso="2026-05-10T00:00:00+00:00",
        soak_end_iso="2026-05-25T00:00:00+00:00",
    )
    assert rows == 3
    assert coin_ids == 1
    await db.close()


@pytest.mark.asyncio
async def test_snapshot_idempotent(tmp_path):
    """Running snapshot twice does not duplicate rows (ON CONFLICT DO NOTHING)."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    async with aiosqlite.connect(str(db_path)) as conn:
        await conn.execute(
            "INSERT INTO slow_burn_candidates "
            "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("coin-b", "BBB", "BetaCoin", 60.0, 0.5, "2026-05-10T03:50:00+00:00"),
        )
        await conn.execute(
            "INSERT INTO volume_history_cg "
            "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("coin-b", "BBB", "BetaCoin", 50000.0, 3e6, 0.2, "2026-05-10T04:00:00+00:00"),
        )
        await conn.commit()

    rows1, _ = await snapshot_volume_history_for_phase_b(
        db,
        soak_start_iso="2026-05-10T00:00:00+00:00",
        soak_end_iso="2026-05-25T00:00:00+00:00",
    )
    rows2, _ = await snapshot_volume_history_for_phase_b(
        db,
        soak_start_iso="2026-05-10T00:00:00+00:00",
        soak_end_iso="2026-05-25T00:00:00+00:00",
    )
    assert rows1 == 1
    assert rows2 == 0  # ON CONFLICT DO NOTHING — no new inserts

    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM audit_volume_snapshot_phase_b WHERE coin_id = ?",
            ("coin-b",),
        )
        count = (await cur.fetchone())[0]
    assert count == 1  # single row, no duplicate

    await db.close()


@pytest.mark.asyncio
async def test_snapshot_filters_out_of_window_detections(tmp_path):
    """Slow_burn detections outside soak window are excluded from cohort."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    async with aiosqlite.connect(str(db_path)) as conn:
        # In-window detection
        await conn.execute(
            "INSERT INTO slow_burn_candidates "
            "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("in-window", "IN", "In", 70.0, 0.0, "2026-05-12T00:00:00+00:00"),
        )
        # Pre-window detection (before 2026-05-10)
        await conn.execute(
            "INSERT INTO slow_burn_candidates "
            "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("pre-window", "PRE", "Pre", 70.0, 0.0, "2026-05-09T00:00:00+00:00"),
        )
        # Post-window detection (after 2026-05-25)
        await conn.execute(
            "INSERT INTO slow_burn_candidates "
            "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("post-window", "POST", "Post", 70.0, 0.0, "2026-05-26T00:00:00+00:00"),
        )
        # Volume rows for all three
        for cid in ("in-window", "pre-window", "post-window"):
            await conn.execute(
                "INSERT INTO volume_history_cg "
                "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cid, cid.upper(), cid, 1000.0, 1e6, 0.1, "2026-05-12T01:00:00+00:00"),
            )
        await conn.commit()

    rows, coin_ids = await snapshot_volume_history_for_phase_b(
        db,
        soak_start_iso="2026-05-10T00:00:00+00:00",
        soak_end_iso="2026-05-25T00:00:00+00:00",
    )
    assert coin_ids == 1  # only "in-window"
    assert rows == 1
    await db.close()


@pytest.mark.asyncio
async def test_snapshot_chunking_boundary(tmp_path):
    """501 distinct coin_ids → 2 chunks (500 + 1), all rows captured."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.connect()

    async with aiosqlite.connect(str(db_path)) as conn:
        for i in range(501):
            coin_id = f"coin-{i:04d}"
            await conn.execute(
                "INSERT INTO slow_burn_candidates "
                "(coin_id, symbol, name, price_change_7d, price_change_1h, detected_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (coin_id, f"S{i:04d}", f"Name{i}", 60.0, 0.0, "2026-05-10T03:50:00+00:00"),
            )
            await conn.execute(
                "INSERT INTO volume_history_cg "
                "(coin_id, symbol, name, volume_24h, market_cap, price, recorded_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (coin_id, f"S{i:04d}", f"Name{i}", 1000.0 + i, 1e6, 0.1, "2026-05-10T04:00:00+00:00"),
            )
        await conn.commit()

    rows, coin_ids = await snapshot_volume_history_for_phase_b(
        db,
        soak_start_iso="2026-05-10T00:00:00+00:00",
        soak_end_iso="2026-05-25T00:00:00+00:00",
    )
    assert rows == 501, f"Expected 501 rows captured across 2 chunks; got {rows}"
    assert coin_ids == 501

    # Verify rows actually landed in audit table (not silently dropped at boundary)
    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute(
            "SELECT COUNT(*) FROM audit_volume_snapshot_phase_b"
        )
        actual_count = (await cur.fetchone())[0]
    assert actual_count == 501, f"audit table has {actual_count} rows; expected 501"

    await db.close()
