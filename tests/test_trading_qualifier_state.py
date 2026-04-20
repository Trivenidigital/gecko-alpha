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
