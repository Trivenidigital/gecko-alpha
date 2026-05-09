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


@pytest.mark.asyncio
async def test_cross_venue_exposure_aggregates_correctly(tmp_path):
    """View must sum + filter correctly: 2 ethereum opens (+7500),
    1 closed sol excluded, 1 coingecko filtered, 1 empty-chain filtered."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    rows = [
        ("t_eth_5000", "ETHTOKA", "ethtoka", "ethereum", 5000.0, "open"),
        ("t_eth_2500", "ETHTOKB", "ethtokb", "ethereum", 2500.0, "open"),
        ("t_sol_99999", "SOLTOK", "soltok", "solana", 99999.0, "closed_tp"),
        ("t_cg_1000", "CGTOK", "cgtok", "coingecko", 1000.0, "open"),
        ("t_empty_500", "ETOK", "etok", "", 500.0, "open"),
    ]
    for token_id, symbol, name, chain, amt, status in rows:
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_price, sl_price,
                status, opened_at)
               VALUES (?, ?, ?, ?, 'first_signal', '{}',
                       100, ?, 10, 120, 80, ?, '2026-05-08T00:00:00+00:00')""",
            (token_id, symbol, name, chain, amt, status),
        )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT venue, open_exposure_usd, open_count "
        "FROM cross_venue_exposure ORDER BY venue"
    )
    by_venue = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}
    assert by_venue["binance"] == (0, 0), "no live_trades opens"
    assert by_venue["minara_ethereum"] == (7500.0, 2), "2 ethereum opens summed"
    assert "minara_solana" not in by_venue, "closed sol must be excluded"
    assert "minara_coingecko" not in by_venue, "coingecko chain must be filtered"
    assert "minara_" not in by_venue, "empty chain must be filtered"
    await db.close()


@pytest.mark.asyncio
async def test_live_trades_has_telemetry_columns(tmp_path):
    """Task 7.5: bl_live_trades_telemetry_v1 migration adds 3 columns."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(live_trades)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "fill_slippage_bps" in cols
    assert "correction_at" in cols
    assert "correction_reason" in cols
    await db.close()


@pytest.mark.asyncio
async def test_signal_venue_correction_count_table_exists(tmp_path):
    """Task 7.5: bl_live_trades_telemetry_v1 migration creates counter table."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='signal_venue_correction_count'"
    )
    assert (await cur.fetchone()) is not None
    cur = await db._conn.execute("PRAGMA table_info(signal_venue_correction_count)")
    cols = {row[1] for row in await cur.fetchall()}
    expected = {
        "signal_type",
        "venue",
        "consecutive_no_correction",
        "last_corrected_at",
        "last_updated_at",
    }
    assert expected <= cols
    await db.close()


@pytest.mark.asyncio
async def test_telemetry_migration_idempotent(tmp_path):
    """Re-running the telemetry migration is a no-op."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._migrate_live_trades_telemetry()  # second call
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name = ?",
        ("bl_live_trades_telemetry_v1",),
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


@pytest.mark.asyncio
async def test_reject_reason_extend_migration_recorded(tmp_path):
    """V3 reviewer C-1 fix: bl_reject_reason_extend_v1 migration runs +
    stamps marker. Fresh DBs already have the 16-value CHECK via
    _create_tables, so the migration is a no-op-but-stamped path."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name = ?",
        ("bl_reject_reason_extend_v1",),
    )
    assert (await cur.fetchone())[0] == 1
    cur = await db._conn.execute(
        "SELECT version FROM schema_version WHERE description = ?",
        ("bl_reject_reason_extend_v1",),
    )
    assert (await cur.fetchone())[0] == 20260512
    await db.close()


@pytest.mark.asyncio
async def test_reject_reason_check_accepts_new_values_on_fresh_db(tmp_path):
    """V3 reviewer C-1 fix: new reject_reasons (signal_disabled,
    notional_cap_exceeded, etc.) must be acceptable INSERTs on fresh
    DBs after migrations run. Verifies the CHECK constraint was either
    extended in CREATE TABLE OR rebuilt by the migration."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Seed paper_trade so live_trades FK is satisfied
    cur = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES ('tok', 'X', 'x', 'ethereum', 'first_signal', '{}',
                   100, 50, 0.5, 120, 80, 'open',
                   '2026-05-08T00:00:00+00:00')"""
    )
    paper_id = cur.lastrowid
    for new_reason in (
        "signal_disabled",
        "notional_cap_exceeded",
        "token_aggregate",
        "master_kill",
    ):
        await db._conn.execute(
            """INSERT INTO live_trades
               (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
                size_usd, status, reject_reason, created_at)
               VALUES (?, 'x', 'X', 'binance', 'XUSDT', 'first_signal',
                       '50', 'rejected', ?, ?)""",
            (paper_id, new_reason, "2026-05-08T00:00:00+00:00"),
        )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM live_trades WHERE reject_reason IN "
        "('signal_disabled','notional_cap_exceeded','token_aggregate','master_kill')"
    )
    assert (await cur.fetchone())[0] == 4
    await db.close()


@pytest.mark.asyncio
async def test_reject_reason_extend_v2_migration_recorded(tmp_path):
    """M1.5a (R1-I1 + R2-I3) — bl_reject_reason_extend_v2 migration runs +
    stamps marker. Fresh DBs already have the 18-value CHECK via
    _create_tables; migration is no-op-but-stamped."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name = ?",
        ("bl_reject_reason_extend_v2",),
    )
    assert (await cur.fetchone())[0] == 1
    cur = await db._conn.execute(
        "SELECT version FROM schema_version WHERE description = ?",
        ("bl_reject_reason_extend_v2",),
    )
    assert (await cur.fetchone())[0] == 20260514
    await db.close()


@pytest.mark.asyncio
async def test_reject_reason_check_accepts_m1_5a_new_values(tmp_path):
    """M1.5a — live_signed_disabled + api_key_lacks_trade_scope must be
    acceptable INSERTs after migrations. Closes the kill-switch state +
    scope-disambiguation gaps R1-I1 and R2-I3 caught."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_price, sl_price,
            status, opened_at)
           VALUES ('tok-m1-5a', 'X', 'x', 'ethereum', 'first_signal', '{}',
                   100, 50, 0.5, 120, 80, 'open',
                   '2026-05-09T00:00:00+00:00')"""
    )
    paper_id = cur.lastrowid
    for new_reason in ("live_signed_disabled", "api_key_lacks_trade_scope"):
        await db._conn.execute(
            """INSERT INTO live_trades
               (paper_trade_id, coin_id, symbol, venue, pair, signal_type,
                size_usd, status, reject_reason, created_at)
               VALUES (?, 'x', 'X', 'binance', 'XUSDT', 'first_signal',
                       '50', 'rejected', ?, '2026-05-09T00:00:00+00:00')""",
            (paper_id, new_reason),
        )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM live_trades WHERE reject_reason IN "
        "('live_signed_disabled','api_key_lacks_trade_scope')"
    )
    assert (await cur.fetchone())[0] == 2
    await db.close()
