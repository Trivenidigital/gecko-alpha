"""Unit tests for scout.trading.qualifier_state (BL-050)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database


async def test_schema_creates_signal_qualifier_state_table(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='signal_qualifier_state'"
    )
    row = await cursor.fetchone()
    assert row is not None, "signal_qualifier_state table must exist after initialize()"

    cursor = await db._conn.execute("PRAGMA table_info(signal_qualifier_state)")
    cols = {r[1]: r[2] for r in await cursor.fetchall()}
    assert cols == {
        "signal_type": "TEXT",
        "token_id": "TEXT",
        "first_qualified_at": "TEXT",
        "last_qualified_at": "TEXT",
    }

    cursor = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_sqs_last_qualified_at'"
    )
    assert await cursor.fetchone() is not None
    await db.close()


def test_config_defaults_for_qualifier_settings(settings_factory):
    s = settings_factory()
    assert s.QUALIFIER_EXIT_GRACE_HOURS == 48
    assert s.QUALIFIER_PRUNE_RETENTION_HOURS == 168
    assert s.QUALIFIER_PRUNE_EVERY_CYCLES == 100


def test_config_rejects_retention_le_grace(settings_factory):
    with pytest.raises(ValueError, match="QUALIFIER_PRUNE_RETENTION_HOURS"):
        settings_factory(
            QUALIFIER_EXIT_GRACE_HOURS=48,
            QUALIFIER_PRUNE_RETENTION_HOURS=48,
        )
    with pytest.raises(ValueError, match="QUALIFIER_PRUNE_RETENTION_HOURS"):
        settings_factory(
            QUALIFIER_EXIT_GRACE_HOURS=48,
            QUALIFIER_PRUNE_RETENTION_HOURS=24,
        )
