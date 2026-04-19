"""Tests for analytics queries (spec §5.1, §7)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import analytics


async def _seed_combo_row(db, key, window, trades, wr, pnl=0.0):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT OR REPLACE INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?, 0, 0, ?)",
        (
            key,
            window,
            trades,
            int(trades * wr / 100),
            trades - int(trades * wr / 100),
            pnl,
            wr,
            now,
        ),
    )
    await db._conn.commit()


async def test_combo_leaderboard_filters_by_min_trades(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_combo_row(db, "big", "30d", trades=30, wr=70.0)
    await _seed_combo_row(db, "tiny", "30d", trades=5, wr=99.0)
    rows = await analytics.combo_leaderboard(db, "30d", min_trades=10)
    keys = [r["combo_key"] for r in rows]
    assert "big" in keys
    assert "tiny" not in keys
    await db.close()


async def test_combo_leaderboard_sort_order(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Tie on WR → tie-break by trades DESC → tie-break by combo_key ASC.
    await _seed_combo_row(db, "bb", "30d", trades=20, wr=50.0)
    await _seed_combo_row(db, "aa", "30d", trades=20, wr=50.0)
    await _seed_combo_row(db, "cc", "30d", trades=30, wr=50.0)  # more trades wins
    rows = await analytics.combo_leaderboard(db, "30d", min_trades=10)
    assert [r["combo_key"] for r in rows] == ["cc", "aa", "bb"]
    await db.close()


async def _seed_gainers_snapshot(db, coin_id, snapshot_at, price_change_24h, mcap):
    await db._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, market_cap, price_change_24h, "
        " price_at_snapshot, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?, 1.0, ?)",
        (
            coin_id,
            coin_id.upper(),
            coin_id.title(),
            mcap,
            price_change_24h,
            snapshot_at.isoformat(),
        ),
    )
    await db._conn.commit()


async def _seed_paper_trade(db, coin_id, opened_at):
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, opened_at, signal_combo) "
        "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
        " 1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, 'open', ?, 'volume_spike')",
        (coin_id, opened_at.isoformat()),
    )
    await db._conn.commit()


async def test_missed_winner_tier_boundaries(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # All above mcap filter, all uncaught.
    cases = [
        ("partial_edge", 50.0, "partial_miss"),
        ("partial_hi", 199.99, "partial_miss"),
        ("major_lo", 200.0, "major_miss"),
        ("major_hi", 999.99, "major_miss"),
        ("disaster", 1000.0, "disaster_miss"),
        ("disaster_big", 2500.0, "disaster_miss"),
    ]
    for coin, pct, _ in cases:
        await _seed_gainers_snapshot(
            db,
            coin,
            now - timedelta(hours=5),
            pct,
            mcap=10_000_000,
        )
    result = await analytics.audit_missed_winners(
        db,
        start=now - timedelta(days=1),
        end=now,
        settings=s,
    )
    buckets = {
        coin: tier
        for tier in ("partial_miss", "major_miss", "disaster_miss")
        for coin in [e["coin_id"] for e in result["tiers"][tier]]
    }
    for coin, _, expected in cases:
        assert buckets[coin] == expected
    await db.close()


async def test_missed_winner_filters_excludes_small_caps(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # mcap too small
    await _seed_gainers_snapshot(
        db,
        "toosmall",
        now - timedelta(hours=2),
        price_change_24h=300,
        mcap=4_999_999,
    )
    # qualifies
    await _seed_gainers_snapshot(
        db,
        "good",
        now - timedelta(hours=2),
        price_change_24h=300,
        mcap=10_000_000,
    )
    result = await analytics.audit_missed_winners(
        db,
        start=now - timedelta(days=1),
        end=now,
        settings=s,
    )
    missed = [e["coin_id"] for tier in result["tiers"].values() for e in tier]
    assert "good" in missed
    assert "toosmall" not in missed
    assert result["denominator"]["winners_filtered_by_mcap"] >= 1
    await db.close()


async def test_missed_winner_catch_window_boundaries(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    crossed = now - timedelta(hours=5)
    # Opened exactly at -30min (should count as caught).
    await _seed_gainers_snapshot(db, "caught_edge", crossed, 300, 10_000_000)
    await _seed_paper_trade(db, "caught_edge", crossed - timedelta(minutes=30))
    # Opened at -31min (missed).
    await _seed_gainers_snapshot(db, "missed_edge", crossed, 300, 10_000_000)
    await _seed_paper_trade(db, "missed_edge", crossed - timedelta(minutes=31))
    result = await analytics.audit_missed_winners(
        db,
        start=now - timedelta(days=1),
        end=now,
        settings=s,
    )
    missed_ids = [e["coin_id"] for tier in result["tiers"].values() for e in tier]
    assert "missed_edge" in missed_ids
    assert "caught_edge" not in missed_ids
    await db.close()


async def test_missed_winner_catch_window_plus_side(tmp_path, settings_factory):
    """Spec §7: window is ±N minutes around crossed_at.

    A trade opened AFTER the crossed_at (up to +window_min) still counts as
    caught. Beyond +window_min it's missed.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    crossed = now - timedelta(hours=5)
    # Opened at +30min → caught (inclusive boundary).
    await _seed_gainers_snapshot(db, "caught_plus", crossed, 300, 10_000_000)
    await _seed_paper_trade(db, "caught_plus", crossed + timedelta(minutes=30))
    # Opened at +31min → missed.
    await _seed_gainers_snapshot(db, "missed_plus", crossed, 300, 10_000_000)
    await _seed_paper_trade(db, "missed_plus", crossed + timedelta(minutes=31))
    result = await analytics.audit_missed_winners(
        db,
        start=now - timedelta(days=1),
        end=now,
        settings=s,
    )
    missed_ids = [e["coin_id"] for tier in result["tiers"].values() for e in tier]
    assert "missed_plus" in missed_ids
    assert "caught_plus" not in missed_ids
    await db.close()


async def test_multi_snapshot_same_coin_uses_min_crossed_at(tmp_path, settings_factory):
    """When a coin appears in multiple snapshots we dedupe by coin_id and
    crossed_at = MIN(snapshot_at) so the catch-window aligns with the
    first time it crossed the winner threshold."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    first_cross = now - timedelta(hours=10)
    later_peak = now - timedelta(hours=3)
    # Three snapshots of the same coin — must dedupe.
    await _seed_gainers_snapshot(db, "bigcoin", first_cross, 250, 10_000_000)
    await _seed_gainers_snapshot(
        db, "bigcoin", now - timedelta(hours=7), 400, 10_000_000
    )
    await _seed_gainers_snapshot(db, "bigcoin", later_peak, 900, 10_000_000)
    # Trade opened at first_cross + 20min → should be caught.
    await _seed_paper_trade(db, "bigcoin", first_cross + timedelta(minutes=20))
    result = await analytics.audit_missed_winners(
        db,
        start=now - timedelta(days=1),
        end=now,
        settings=s,
    )
    missed_ids = [e["coin_id"] for tier in result["tiers"].values() for e in tier]
    caught_count = result["denominator"]["winners_caught"]
    # Confirm: single entry only (not duplicated), caught against first crossed_at.
    assert "bigcoin" not in missed_ids
    assert caught_count == 1
    await db.close()


async def test_empty_denominator_emits_warning(tmp_path, settings_factory, caplog):
    """When no winners qualify (empty denominator) we log a warning, not crash."""
    import structlog.testing

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # No snapshots at all.
    with structlog.testing.capture_logs() as caplog_entries:
        result = await analytics.audit_missed_winners(
            db,
            start=now - timedelta(days=1),
            end=now,
            settings=s,
        )
    assert result["denominator"]["winners_total"] == 0
    assert result["denominator"]["winners_missed"] == 0
    assert any(e.get("event") == "audit_query_empty_warning" for e in caplog_entries)
    await db.close()


async def _seed_lead_trade(db, coin, opened_at, lead, status):
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, opened_at, signal_combo, "
        " lead_time_vs_trending_min, lead_time_vs_trending_status) "
        "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
        " 1.0, 100, 100, 20, 10, 1.2, 0.9, 'open', ?, 'volume_spike', ?, ?)",
        (coin, opened_at.isoformat(), lead, status),
    )


async def test_lead_time_breakdown_filters_status_ok(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Seed trades with mixed statuses.
    now = datetime.now(timezone.utc)
    await _seed_lead_trade(db, "a", now, -10.0, "ok")
    await _seed_lead_trade(db, "b", now, -20.0, "ok")
    await _seed_lead_trade(db, "c", now, None, "no_reference")
    await _seed_lead_trade(db, "d", now, None, "error")
    await db._conn.commit()

    result = await analytics.lead_time_breakdown(db, window="30d")
    row = result["volume_spike"]
    assert row["count_ok"] == 2
    assert row["count_no_reference"] == 1
    assert row["count_error"] == 1
    # D21 percentile-in-Python: for n=2 sorted [-20, -10], median = values[n//2] = values[1] = -10.
    assert row["median_min"] == -10.0


async def test_lead_time_breakdown_exact_percentiles(tmp_path):
    """D21: percentile logic lives in Python. Given known inputs, confirm exact values.

    For n=5 sorted [-50, -40, -30, -20, -10]:
      median = values[n // 2] = values[2] = -30
      p25    = values[max(n // 4, 0)] = values[1] = -40
      p75    = values[min((3 * n) // 4, n - 1)] = values[3] = -20
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    for coin, lead in [
        ("a", -10.0),
        ("b", -20.0),
        ("c", -30.0),
        ("d", -40.0),
        ("e", -50.0),
    ]:
        await _seed_lead_trade(db, coin, now, lead, "ok")
    await db._conn.commit()

    result = await analytics.lead_time_breakdown(db, window="30d")
    row = result["volume_spike"]
    assert row["count_ok"] == 5
    assert row["median_min"] == -30.0
    assert row["p25_min"] == -40.0
    assert row["p75_min"] == -20.0
    await db.close()


async def test_detect_pipeline_gaps(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    # Snapshots at -4h, -3h, -1h, -0.5h  →  gap between -3h and -1h (2h > 60min).
    # -4h to -3h = 1h = exactly 60min (NOT > threshold), so only 1 gap.
    for hours in (4, 3, 1, 0.5):
        await db._conn.execute(
            "INSERT INTO gainers_snapshots "
            "(coin_id, symbol, name, market_cap, "
            " price_change_24h, price_at_snapshot, snapshot_at) "
            "VALUES ('x', 'X', 'X', 1e7, 10.0, 1.0, ?)",
            ((now - timedelta(hours=hours)).isoformat(),),
        )
    await db._conn.commit()
    gaps = await analytics.detect_pipeline_gaps(
        db,
        start=now - timedelta(days=1),
        end=now,
        max_gap_minutes=60,
    )
    assert len(gaps) == 1
    await db.close()


async def test_lead_time_breakdown_unknown_status_counted_as_error(tmp_path):
    """Rows with unexpected status values must land in the error bucket, not be silently
    dropped. This ensures corrupted data is visible in the weekly digest."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)

    # 1 valid ok, 1 valid error, 1 with nonsense status.
    await _seed_lead_trade(db, "a", now, -5.0, "ok")
    await _seed_lead_trade(db, "b", now, None, "error")
    await _seed_lead_trade(db, "c", now, None, "GARBAGE_STATUS_XYZ_UNKNOWN")
    await db._conn.commit()

    result = await analytics.lead_time_breakdown(db, window="30d")
    row = result["volume_spike"]

    assert row["count_ok"] == 1
    # Both the explicit 'error' and the unknown status must contribute to error bucket.
    assert (
        row["count_error"] == 2
    ), f"Expected count_error=2 (explicit + unknown), got {row['count_error']}"
    await db.close()
