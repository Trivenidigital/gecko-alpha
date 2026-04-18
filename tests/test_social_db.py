"""Tests for LunarCrush social-integration DB schema.

Validates that ``Database.initialize()`` creates the new tables
(``social_signals``, ``social_baselines``, ``social_credit_ledger``)
with the expected columns and constraints, and adds the additive
``detected_by_social`` / ``social_detected_at`` / ``social_lead_minutes``
columns to ``trending_comparisons``.
"""

from __future__ import annotations

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "social.db")
    await d.initialize()
    yield d
    await d.close()


async def _columns(db: Database, table: str) -> set[str]:
    cursor = await db._conn.execute(f"PRAGMA table_info({table})")
    return {row[1] for row in await cursor.fetchall()}


@pytest.mark.asyncio
async def test_social_signals_table_created(db):
    """social_signals is created with the expected fired_* flag columns."""
    cols = await _columns(db, "social_signals")
    # Core identity
    assert "coin_id" in cols
    assert "symbol" in cols
    assert "name" in cols
    # Per-kind fire flags replace v1 CSV
    assert "fired_social_volume_24h" in cols
    assert "fired_galaxy_jump" in cols
    assert "fired_interactions_accel" in cols
    # Numeric context
    assert "galaxy_score" in cols
    assert "social_volume_24h" in cols
    assert "social_volume_baseline" in cols
    assert "social_spike_ratio" in cols
    assert "interactions_24h" in cols
    assert "sentiment" in cols
    assert "social_dominance" in cols
    assert "price_change_1h" in cols
    assert "price_change_24h" in cols
    assert "market_cap" in cols
    assert "current_price" in cols
    assert "detected_at" in cols
    assert "alerted_at" in cols
    assert "created_at" in cols


@pytest.mark.asyncio
async def test_social_signals_unique_coin_detected_at(db):
    """The UNIQUE(coin_id, detected_at) constraint deduplicates."""
    await db._conn.execute(
        """INSERT INTO social_signals
           (coin_id, symbol, name, detected_at)
           VALUES ('foo', 'FOO', 'Foo', '2026-04-18T12:00:00+00:00')"""
    )
    # Second INSERT OR IGNORE with the same pair should be a no-op.
    await db._conn.execute(
        """INSERT OR IGNORE INTO social_signals
           (coin_id, symbol, name, detected_at)
           VALUES ('foo', 'FOO', 'Foo', '2026-04-18T12:00:00+00:00')"""
    )
    await db._conn.commit()
    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM social_signals WHERE coin_id = 'foo'"
    )
    assert (await cursor.fetchone())[0] == 1


@pytest.mark.asyncio
async def test_social_baselines_table_created(db):
    cols = await _columns(db, "social_baselines")
    assert "coin_id" in cols
    assert "symbol" in cols
    assert "avg_social_volume_24h" in cols
    assert "avg_galaxy_score" in cols
    assert "last_galaxy_score" in cols
    assert "interactions_ring" in cols
    assert "sample_count" in cols
    assert "last_poll_at" in cols
    assert "last_updated" in cols


@pytest.mark.asyncio
async def test_social_credit_ledger_table_created(db):
    cols = await _columns(db, "social_credit_ledger")
    assert "utc_date" in cols
    assert "credits_used" in cols
    assert "last_updated" in cols


@pytest.mark.asyncio
async def test_trending_comparisons_has_social_columns(db):
    """Additive migration exposes detected_by_social + lead columns."""
    cols = await _columns(db, "trending_comparisons")
    assert "detected_by_social" in cols
    assert "social_detected_at" in cols
    assert "social_lead_minutes" in cols


@pytest.mark.asyncio
async def test_migration_is_idempotent(tmp_path):
    """Re-opening the DB doesn't fail even though columns/tables already exist."""
    path = tmp_path / "idempotent.db"
    for _ in range(3):
        d = Database(path)
        await d.initialize()
        await d.close()
    # If we got here, the ADD COLUMN / CREATE TABLE IF NOT EXISTS migrations
    # tolerated being run more than once.
