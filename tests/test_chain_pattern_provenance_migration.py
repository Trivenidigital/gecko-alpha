import json

import aiosqlite
import pytest

from scout.chains.patterns import seed_built_in_patterns
from scout.db import Database


async def _columns(db: Database) -> set[str]:
    async with db._conn.execute("PRAGMA table_info(chain_patterns)") as cur:
        return {row["name"] for row in await cur.fetchall()}


@pytest.mark.asyncio
async def test_fresh_db_has_chain_pattern_provenance_columns(tmp_path):
    db = Database(tmp_path / "fresh.db")
    await db.initialize()
    try:
        cols = await _columns(db)
        assert {"is_protected_builtin", "disabled_reason", "disabled_at"} <= cols
        async with db._conn.execute(
            "SELECT name FROM paper_migrations WHERE name='bl_chain_pattern_provenance_v1'"
        ) as cur:
            assert await cur.fetchone() is not None
        async with db._conn.execute(
            "SELECT description FROM schema_version WHERE version=20260520"
        ) as cur:
            row = await cur.fetchone()
        assert row is not None
        assert row["description"] == "bl_chain_pattern_provenance_v1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_existing_db_migrates_chain_pattern_provenance_columns(tmp_path):
    path = tmp_path / "existing.db"
    db = Database(path)
    await db.initialize()
    await seed_built_in_patterns(db)
    await db.close()

    async with aiosqlite.connect(path) as conn:
        await conn.execute("ALTER TABLE chain_patterns DROP COLUMN is_protected_builtin")
        await conn.execute("ALTER TABLE chain_patterns DROP COLUMN disabled_reason")
        await conn.execute("ALTER TABLE chain_patterns DROP COLUMN disabled_at")
        await conn.commit()

    db2 = Database(path)
    await db2.initialize()
    try:
        cols = await _columns(db2)
        assert {"is_protected_builtin", "disabled_reason", "disabled_at"} <= cols
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_prod_snapshot_inactive_builtins_stamp_legacy_lifecycle(tmp_path):
    db = Database(tmp_path / "snapshot.db")
    await db.initialize()
    await seed_built_in_patterns(db)
    stats = {
        "full_conviction": (52, 2),
        "narrative_momentum": (58, 2),
        "volume_breakout": (70, 3),
    }
    for name, (triggers, hits) in stats.items():
        await db._conn.execute(
            """UPDATE chain_patterns
               SET is_active=0,
                   historical_hit_rate=?,
                   total_triggers=?,
                   total_hits=?,
                   updated_at='2026-05-17 01:24:59'
               WHERE name=?""",
            (hits / triggers, triggers, hits, name),
        )
    await db._conn.commit()
    await db.close()

    async with aiosqlite.connect(db._db_path) as conn:
        await conn.execute("ALTER TABLE chain_patterns DROP COLUMN is_protected_builtin")
        await conn.execute("ALTER TABLE chain_patterns DROP COLUMN disabled_reason")
        await conn.execute("ALTER TABLE chain_patterns DROP COLUMN disabled_at")
        await conn.execute(
            "DELETE FROM paper_migrations WHERE name='bl_chain_pattern_provenance_v1'"
        )
        await conn.execute("DELETE FROM schema_version WHERE version=20260520")
        await conn.commit()

    db2 = Database(db._db_path)
    await db2.initialize()
    try:
        async with db2._conn.execute(
            "SELECT name, disabled_reason FROM chain_patterns ORDER BY name"
        ) as cur:
            rows = {row["name"]: row["disabled_reason"] for row in await cur.fetchall()}
        assert rows["full_conviction"] == "legacy_lifecycle_retired"
        assert rows["narrative_momentum"] == "legacy_lifecycle_retired"
        assert rows["volume_breakout"] == "legacy_lifecycle_retired"
    finally:
        await db2.close()


@pytest.mark.asyncio
async def test_non_matching_inactive_builtin_is_not_stamped_legacy(tmp_path):
    db = Database(tmp_path / "not_snapshot.db")
    await db.initialize()
    await seed_built_in_patterns(db)
    await db._conn.execute(
        """UPDATE chain_patterns
           SET is_active=0,
               total_triggers=99,
               total_hits=1,
               historical_hit_rate=?,
               updated_at='2026-05-16 00:00:00'
           WHERE name='full_conviction'""",
        (1 / 99,),
    )
    await db._conn.commit()
    await db.close()

    async with aiosqlite.connect(db._db_path) as conn:
        await conn.execute("ALTER TABLE chain_patterns DROP COLUMN is_protected_builtin")
        await conn.execute("ALTER TABLE chain_patterns DROP COLUMN disabled_reason")
        await conn.execute("ALTER TABLE chain_patterns DROP COLUMN disabled_at")
        await conn.execute(
            "DELETE FROM paper_migrations WHERE name='bl_chain_pattern_provenance_v1'"
        )
        await conn.execute("DELETE FROM schema_version WHERE version=20260520")
        await conn.commit()

    db2 = Database(db._db_path)
    await db2.initialize()
    try:
        async with db2._conn.execute(
            "SELECT disabled_reason FROM chain_patterns WHERE name='full_conviction'"
        ) as cur:
            row = await cur.fetchone()
        assert row["disabled_reason"] is None
    finally:
        await db2.close()
