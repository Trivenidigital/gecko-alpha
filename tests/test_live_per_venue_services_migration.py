"""BL-NEW-LIVE-HYBRID M1: 5 per-venue services tables migration."""

from __future__ import annotations

import pytest

from scout.db import Database


@pytest.mark.asyncio
async def test_all_5_tables_exist(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    expected = {
        "venue_health",
        "wallet_snapshots",
        "venue_listings",
        "venue_rate_state",
        "symbol_aliases",
    }
    cur = await db._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    actual = {row[0] for row in await cur.fetchall()}
    missing = expected - actual
    assert not missing, f"missing tables: {missing}"
    await db.close()


@pytest.mark.asyncio
async def test_venue_health_has_expected_columns(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(venue_health)")
    cols = {row[1] for row in await cur.fetchall()}
    expected = {
        "venue",
        "probe_at",
        "rest_responsive",
        "rest_latency_ms",
        "ws_connected",
        "rate_limit_headroom_pct",
        "auth_ok",
        "last_balance_fetch_ok",
        "last_quote_mid_price",
        "last_quote_at",
        "last_depth_at_size_bps",
        "fills_30d_count",
        "is_dormant",
        "error_text",
    }
    assert expected <= cols, f"missing: {expected - cols}"
    await db.close()


@pytest.mark.asyncio
async def test_venue_listings_unique_per_venue_canonical_class(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute("""INSERT INTO venue_listings
           (venue, canonical, venue_pair, quote, asset_class, refreshed_at)
           VALUES ('binance', 'BTC', 'BTCUSDT', 'USDT', 'perp',
                   '2026-05-08T00:00:00+00:00')""")
    await db._conn.commit()
    with pytest.raises(Exception):  # IntegrityError on duplicate PK
        await db._conn.execute("""INSERT INTO venue_listings
               (venue, canonical, venue_pair, quote, asset_class, refreshed_at)
               VALUES ('binance', 'BTC', 'BTCUSDT', 'USDT', 'perp',
                       '2026-05-08T00:00:01+00:00')""")
        await db._conn.commit()
    await db.close()


@pytest.mark.asyncio
async def test_migration_idempotent(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._migrate_per_venue_services()  # second call
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name = ?",
        ("bl_per_venue_services_v1",),
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


@pytest.mark.asyncio
async def test_cross_venue_exposure_view_exists(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type='view' AND name='cross_venue_exposure'"
    )
    assert (await cur.fetchone()) is not None
    await db.close()


@pytest.mark.asyncio
async def test_cross_venue_pnl_view_exists(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master " "WHERE type='view' AND name='cross_venue_pnl'"
    )
    assert (await cur.fetchone()) is not None
    await db.close()
