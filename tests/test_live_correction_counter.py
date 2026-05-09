"""BL-NEW-LIVE-HYBRID M1.5b: signal_venue_correction_count writer tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.live.correction_counter import (
    increment_consecutive,
    reset_on_correction,
)


@pytest.mark.asyncio
async def test_increment_creates_row_on_first_call(tmp_path):
    """First call for (signal_type, venue) creates a row with
    consecutive_no_correction=1."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await increment_consecutive(db, "first_signal", "binance")
    cur = await db._conn.execute(
        "SELECT consecutive_no_correction FROM signal_venue_correction_count "
        "WHERE signal_type = ? AND venue = ?",
        ("first_signal", "binance"),
    )
    row = await cur.fetchone()
    assert row[0] == 1
    await db.close()


@pytest.mark.asyncio
async def test_increment_bumps_counter_on_subsequent_calls(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(5):
        await increment_consecutive(db, "first_signal", "binance")
    cur = await db._conn.execute(
        "SELECT consecutive_no_correction FROM signal_venue_correction_count "
        "WHERE signal_type = ? AND venue = ?",
        ("first_signal", "binance"),
    )
    row = await cur.fetchone()
    assert row[0] == 5
    await db.close()


@pytest.mark.asyncio
async def test_reset_zeros_counter_and_records_correction_at(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for _ in range(10):
        await increment_consecutive(db, "first_signal", "binance")
    correction_at = datetime.now(timezone.utc).isoformat()
    await reset_on_correction(db, "first_signal", "binance", correction_at)
    cur = await db._conn.execute(
        "SELECT consecutive_no_correction, last_corrected_at "
        "FROM signal_venue_correction_count "
        "WHERE signal_type = ? AND venue = ?",
        ("first_signal", "binance"),
    )
    row = await cur.fetchone()
    assert row[0] == 0
    assert row[1] == correction_at
    await db.close()


@pytest.mark.asyncio
async def test_increment_independent_per_signal_venue_pair(tmp_path):
    """(first_signal × binance) and (first_signal × kraken) have
    independent counters."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await increment_consecutive(db, "first_signal", "binance")
    await increment_consecutive(db, "first_signal", "kraken")
    await increment_consecutive(db, "first_signal", "binance")
    cur = await db._conn.execute(
        "SELECT venue, consecutive_no_correction "
        "FROM signal_venue_correction_count "
        "WHERE signal_type = ? ORDER BY venue",
        ("first_signal",),
    )
    rows = await cur.fetchall()
    by_venue = {r[0]: r[1] for r in rows}
    assert by_venue == {"binance": 2, "kraken": 1}
    await db.close()


@pytest.mark.asyncio
async def test_increment_handles_none_signal_type(tmp_path):
    """R1-I7 fold: signal_type=None coerced to 'unknown' (not crash, not
    silent skip)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await increment_consecutive(db, None, "binance")
    cur = await db._conn.execute(
        "SELECT signal_type, consecutive_no_correction "
        "FROM signal_venue_correction_count WHERE venue = ?",
        ("binance",),
    )
    row = await cur.fetchone()
    assert row[0] == "unknown"
    assert row[1] == 1
    await db.close()


@pytest.mark.asyncio
async def test_increment_handles_empty_signal_type(tmp_path):
    """R1-I7 fold: empty-string signal_type coerced to 'unknown'."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await increment_consecutive(db, "", "binance")
    cur = await db._conn.execute(
        "SELECT signal_type FROM signal_venue_correction_count WHERE venue = ?",
        ("binance",),
    )
    row = await cur.fetchone()
    assert row[0] == "unknown"
    await db.close()
