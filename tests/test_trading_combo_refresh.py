"""Tests for nightly combo refresh (spec §5.3)."""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import combo_refresh

_counter = itertools.count()


async def _insert_trade(
    db,
    combo_key: str,
    pnl_usd: float,
    pnl_pct: float,
    closed_at: datetime,
    status: str = "closed_tp",
    opened_at: datetime | None = None,
):
    opened = (opened_at or closed_at - timedelta(hours=1)).isoformat()
    # Unique token_id via counter to avoid UNIQUE(token_id, signal_type, opened_at)
    token_id = f"tok_{combo_key}_{next(_counter)}"
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, pnl_usd, pnl_pct, opened_at, closed_at, signal_combo) "
        "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
        " 1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, ?, ?, ?, ?, ?, ?)",
        (token_id, status, pnl_usd, pnl_pct, opened, closed_at.isoformat(), combo_key),
    )
    await db._conn.commit()


async def _get_combo_row(db, combo_key, window):
    cur = await db._conn.execute(
        "SELECT trades, wins, losses, win_rate_pct, avg_pnl_pct, "
        "       suppressed, parole_at, parole_trades_remaining, refresh_failures "
        "FROM combo_performance WHERE combo_key = ? AND window = ?",
        (combo_key, window),
    )
    return await cur.fetchone()


async def test_refresh_computes_7d_and_30d_rollup(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # 3 wins, 2 losses in last 3 days
    for pnl in [10, 20, 30]:
        await _insert_trade(db, "combo_x", pnl, 5.0, now - timedelta(days=1))
    for pnl in [-5, -10]:
        await _insert_trade(db, "combo_x", pnl, -3.0, now - timedelta(days=1))
    ok = await combo_refresh.refresh_combo(db, "combo_x", s)
    assert ok

    row = await _get_combo_row(db, "combo_x", "7d")
    assert row["trades"] == 5
    assert row["wins"] == 3
    assert row["losses"] == 2
    assert abs(row["win_rate_pct"] - 60.0) < 0.01

    row30 = await _get_combo_row(db, "combo_x", "30d")
    assert row30["trades"] == 5

    await db.close()


async def test_suppression_not_triggered_at_boundary_wr_eq_30(
    tmp_path, settings_factory
):
    """trades=20 AND wr=30.0 → NOT suppressed (strict inequality)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    for _ in range(6):
        await _insert_trade(db, "boundary", 10, 5.0, now - timedelta(days=2))
    for _ in range(14):
        await _insert_trade(db, "boundary", -5, -3.0, now - timedelta(days=2))
    await combo_refresh.refresh_combo(db, "boundary", s)
    row = await _get_combo_row(db, "boundary", "30d")
    assert row["trades"] == 20
    assert abs(row["win_rate_pct"] - 30.0) < 0.01
    assert row["suppressed"] == 0
    await db.close()


async def test_suppression_triggered_at_wr_just_below_30(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # 5 wins out of 20 = 25% WR
    for _ in range(5):
        await _insert_trade(db, "loser", 10, 5.0, now - timedelta(days=2))
    for _ in range(15):
        await _insert_trade(db, "loser", -5, -3.0, now - timedelta(days=2))
    await combo_refresh.refresh_combo(db, "loser", s)
    row = await _get_combo_row(db, "loser", "30d")
    assert row["suppressed"] == 1
    assert row["parole_at"] is not None
    assert row["parole_trades_remaining"] == s.FEEDBACK_PAROLE_RETEST_TRADES
    await db.close()


async def test_suppression_not_triggered_when_trades_below_min(
    tmp_path, settings_factory
):
    """trades=19 must NOT trigger suppression even at 0% WR."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    for _ in range(19):
        await _insert_trade(db, "small", -5, -3.0, now - timedelta(days=2))
    await combo_refresh.refresh_combo(db, "small", s)
    row = await _get_combo_row(db, "small", "30d")
    assert row["suppressed"] == 0
    await db.close()


async def test_parole_auto_clear_on_wr_recovery(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # Pre-seed: combo is on parole with remaining=0.
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, parole_at, "
        " parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES ('recovered', '30d', 25, 5, 20, -100.0, -2.0, 20.0, 1, ?, ?, 0, 0, ?)",
        (
            (now - timedelta(days=15)).isoformat(),
            (now - timedelta(days=1)).isoformat(),
            now.isoformat(),
        ),
    )
    await db._conn.commit()
    # Add recent winning trades for recovery
    for _ in range(15):
        await _insert_trade(db, "recovered", 10, 5.0, now - timedelta(days=1))
    await combo_refresh.refresh_combo(db, "recovered", s)
    row = await _get_combo_row(db, "recovered", "30d")
    # With wr >= 30 and parole_trades_remaining=0: clear suppression.
    assert row["suppressed"] == 0
    assert row["parole_at"] is None
    assert row["parole_trades_remaining"] is None
    await db.close()


async def test_re_suppression_resets_timestamps(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    old_suppressed_at = (now - timedelta(days=20)).isoformat()
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, parole_at, "
        " parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES ('re_supp', '30d', 25, 5, 20, -50, -2, 20.0, 1, ?, ?, 0, 0, ?)",
        (old_suppressed_at, (now - timedelta(days=1)).isoformat(), now.isoformat()),
    )
    await db._conn.commit()
    # Recent trades still poor
    for _ in range(20):
        await _insert_trade(db, "re_supp", -5, -3, now - timedelta(days=2))
    await combo_refresh.refresh_combo(db, "re_supp", s)
    row = await _get_combo_row(db, "re_supp", "30d")
    assert row["suppressed"] == 1
    assert row["parole_trades_remaining"] == s.FEEDBACK_PAROLE_RETEST_TRADES
    cur = await db._conn.execute(
        "SELECT suppressed_at FROM combo_performance WHERE combo_key = 're_supp'"
    )
    new_suppressed_at = (await cur.fetchone())[0]
    assert new_suppressed_at != old_suppressed_at
    await db.close()


async def test_refresh_all_aggregates_distinct_combos(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    await _insert_trade(db, "c1", 10, 5.0, now - timedelta(days=1))
    await _insert_trade(db, "c2", 20, 5.0, now - timedelta(days=1))
    summary = await combo_refresh.refresh_all(db, s)
    assert summary["refreshed"] == 2
    assert summary["failed"] == 0
    await db.close()


async def test_window_cutoff_7d_excludes_old_trades(tmp_path, settings_factory):
    """A trade closed 8 days ago must appear in 30d but NOT 7d."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    await _insert_trade(db, "wc", 100, 10.0, now - timedelta(days=8))
    await _insert_trade(db, "wc", 100, 10.0, now - timedelta(days=2))
    await combo_refresh.refresh_combo(db, "wc", s)
    row_7d = await _get_combo_row(db, "wc", "7d")
    row_30d = await _get_combo_row(db, "wc", "30d")
    assert row_7d["trades"] == 1, "8-day-old trade must be excluded from 7d"
    assert row_30d["trades"] == 2, "8-day-old trade must be included in 30d"
    await db.close()


async def test_window_cutoff_30d_excludes_very_old_trades(tmp_path, settings_factory):
    """A trade closed 31 days ago must NOT appear in 30d."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    await _insert_trade(db, "old", 100, 10.0, now - timedelta(days=31))
    await _insert_trade(db, "old", 100, 10.0, now - timedelta(days=2))
    await combo_refresh.refresh_combo(db, "old", s)
    row_30d = await _get_combo_row(db, "old", "30d")
    assert row_30d["trades"] == 1
    await db.close()


async def test_zero_trade_combo_writes_empty_row(tmp_path, settings_factory):
    """A combo with no closed trades in window — no error, trades=0, not suppressed."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    ok = await combo_refresh.refresh_combo(db, "empty", s)
    assert ok is True
    row = await _get_combo_row(db, "empty", "30d")
    assert row["trades"] == 0
    assert row["suppressed"] == 0
    assert row["win_rate_pct"] == 0.0
    await db.close()


async def test_refresh_failures_increments_on_error(
    tmp_path, settings_factory, monkeypatch
):
    """When refresh_combo raises, refresh_failures must increment (so chronic
    failures surface in the weekly digest). HIGH-6 regression gate.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    await _insert_trade(db, "flaky", 10, 5.0, now - timedelta(days=1))

    # First do a successful refresh so the row exists with refresh_failures=0.
    assert await combo_refresh.refresh_combo(db, "flaky", s) is True

    # Now monkeypatch to force an exception during the main UPSERT. The SELECT
    # is cheap and happens first; fail on the INSERT so the try: body aborts
    # and enters the except path.
    original_execute = db._conn.execute
    import aiosqlite

    async def _fail_on_upsert(sql, *args, **kwargs):
        if "INSERT INTO combo_performance" in str(sql) and "'7d'" in str(sql):
            raise aiosqlite.OperationalError("forced failure")
        return await original_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db._conn, "execute", _fail_on_upsert)
    ok = await combo_refresh.refresh_combo(db, "flaky", s)
    assert ok is False

    # Undo monkeypatch and inspect counter.
    monkeypatch.setattr(db._conn, "execute", original_execute)
    row = await _get_combo_row(db, "flaky", "30d")
    assert row["refresh_failures"] >= 1, "refresh_failures must increment on error"
    await db.close()


async def test_refresh_failures_resets_to_zero_on_success(tmp_path, settings_factory):
    """After a failed refresh incremented the counter, a subsequent successful
    refresh must reset it to 0 (UPSERT sets refresh_failures=0).
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # Seed with refresh_failures=5 and one real trade.
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES ('healed', '30d', 0, 0, 0, 0, 0, 0, 0, 5, ?)",
        (now.isoformat(),),
    )
    await db._conn.commit()
    await _insert_trade(db, "healed", 10, 5.0, now - timedelta(days=1))

    ok = await combo_refresh.refresh_combo(db, "healed", s)
    assert ok is True
    row = await _get_combo_row(db, "healed", "30d")
    assert row["refresh_failures"] == 0


async def test_chronic_failure_threshold_detected(tmp_path, settings_factory):
    """refresh_all returns combos whose refresh_failures >= threshold."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # Seed a combo with refresh_failures=3 (== default threshold) and no trades
    # in last 30d, so it won't be picked up by the DISTINCT scan — manually
    # include it via refresh_all's second SELECT which queries combo_performance
    # directly for chronic failures.
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES ('stuck', '30d', 0, 0, 0, 0, 0, 0, 0, 3, ?)",
        (now.isoformat(),),
    )
    await db._conn.commit()

    summary = await combo_refresh.refresh_all(db, s)
    assert "stuck" in summary["chronic_failures"]
    await db.close()


async def test_mid_parole_refresh_preserves_parole_at(tmp_path, settings_factory):
    """If a combo is actively mid-parole (remaining > 0) and WR hasn't recovered,
    refresh_combo must NOT overwrite parole_at — otherwise parole timing resets
    every nightly refresh and the combo never exits the parole window."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT=30)
    now = datetime.now(timezone.utc)
    # Seed a combo: suppressed=1, parole set 2 days ago, remaining=2, WR=10%
    original_parole = (now - timedelta(days=2)).isoformat()
    await db._conn.execute(
        "INSERT INTO combo_performance (combo_key, window, trades, wins, losses, "
        " total_pnl_usd, avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, "
        " parole_at, parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES ('midpar', '30d', 20, 2, 18, -200, -10, 10.0, 1, ?, ?, 2, 0, ?)",
        (original_parole, original_parole, original_parole),
    )
    await db._conn.commit()
    # Seed 20 closed trades: 2 wins, 18 losses → WR=10% (still poor < 30%)
    for i in range(20):
        status = "closed_tp" if i < 2 else "closed_sl"
        pnl_usd = 10 if i < 2 else -10
        pnl_pct = 10.0 if i < 2 else -10.0
        token_id = f"tm_{i}"
        await db._conn.execute(
            "INSERT INTO paper_trades (token_id, symbol, name, chain, signal_type, "
            " signal_data, entry_price, amount_usd, quantity, tp_pct, sl_pct, "
            " tp_price, sl_price, status, pnl_usd, pnl_pct, opened_at, closed_at, "
            " signal_combo) "
            "VALUES (?, 'S', 'N', 'cg', 'gainers_early', '{}', 1, 100, 100, 20, 10, "
            " 1.2, 0.9, ?, ?, ?, ?, ?, 'midpar')",
            (
                token_id,
                status,
                pnl_usd,
                pnl_pct,
                (now - timedelta(days=3, hours=i)).isoformat(),
                (now - timedelta(days=2, hours=i)).isoformat(),
            ),
        )
    await db._conn.commit()
    await combo_refresh.refresh_combo(db, "midpar", s)
    cur = await db._conn.execute(
        "SELECT parole_at FROM combo_performance WHERE combo_key='midpar' AND window='30d'"
    )
    row = await cur.fetchone()
    assert row["parole_at"] == original_parole, (
        f"parole_at was overwritten: expected {original_parole!r}, got {row['parole_at']!r}"
    )
    await db.close()
