"""BL-064 schema migration tests."""

from __future__ import annotations

from datetime import datetime

import pytest

from scout.db import Database


@pytest.mark.asyncio
async def test_bl064_creates_six_tables(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name LIKE 'tg_social_%' ORDER BY name"
    )
    names = {row[0] for row in await cur.fetchall()}
    assert {
        "tg_social_channels",
        "tg_social_watermarks",
        "tg_social_messages",
        "tg_social_signals",
        "tg_social_health",
        "tg_social_dlq",
    } <= names
    await db.close()


@pytest.mark.asyncio
async def test_bl064_indexes_present(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND name LIKE 'idx_tg_social_%'"
    )
    names = {row[0] for row in await cur.fetchall()}
    assert {
        "idx_tg_social_signals_token_created",
        "idx_tg_social_signals_channel_created",
        "idx_tg_social_signals_paper_trade_id",
        "idx_tg_social_messages_channel_msgid",
        "idx_tg_social_dlq_failed_at",
    } <= names
    await db.close()


@pytest.mark.asyncio
async def test_bl064_cutover_row_inserted(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name = 'bl064_tg_social'"
    )
    row = await cur.fetchone()
    assert row is not None
    parsed = datetime.fromisoformat(row[0])
    assert parsed.tzinfo is not None
    await db.close()


@pytest.mark.asyncio
async def test_bl064_post_assertion_includes_all_four_cutovers(tmp_path):
    """Defense-in-depth: the migration aborts if any of bl061/bl062/bl063/bl064
    rows are missing. Re-running must succeed (idempotent)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db.close()
    db2 = Database(tmp_path / "t.db")
    await db2.initialize()  # second init must NOT fail
    cur = await db2._conn.execute("SELECT name FROM paper_migrations ORDER BY name")
    names = {row[0] for row in await cur.fetchall()}
    assert {
        "bl061_ladder",
        "bl062_peak_fade",
        "bl063_moonshot",
        "bl064_tg_social",
    } <= names
    await db2.close()


@pytest.mark.asyncio
async def test_bl064_pre_existing_rows_have_null(tmp_path):
    """Insert a row directly to simulate pre-migration data, confirm clean state."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Insert a channel and verify defaults
    await db._conn.execute(
        "INSERT INTO tg_social_channels (channel_handle, display_name, added_at) "
        "VALUES ('@test', 'Test', '2026-04-27T00:00:00+00:00')"
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT trade_eligible, removed_at FROM tg_social_channels "
        "WHERE channel_handle = '@test'"
    )
    row = await cur.fetchone()
    assert row[0] == 1  # default trade_eligible
    assert row[1] is None  # not removed
    await db.close()
