"""BL-NEW-LIVE-HYBRID M1: live_eligible column migration."""

from __future__ import annotations

import pytest

from scout.db import Database


@pytest.mark.asyncio
async def test_signal_params_has_live_eligible_column(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(signal_params)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "live_eligible" in cols
    await db.close()


@pytest.mark.asyncio
async def test_live_eligible_defaults_to_0_for_seed_signals(tmp_path):
    """Default fail-closed: every seed row gets live_eligible=0."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("SELECT signal_type, live_eligible FROM signal_params")
    for sig, opt in await cur.fetchall():
        assert opt == 0, f"{sig} should default to 0; got {opt}"
    await db.close()


@pytest.mark.asyncio
async def test_migration_idempotent_on_rerun(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._migrate_live_eligible_column()
    cur = await db._conn.execute("PRAGMA table_info(signal_params)")
    cols = [row[1] for row in await cur.fetchall()]
    assert cols.count("live_eligible") == 1
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name = ?",
        ("bl_live_eligible_v1",),
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()
