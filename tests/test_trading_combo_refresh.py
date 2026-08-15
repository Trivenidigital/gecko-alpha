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
    # Add recent winning trades for recovery. `opened_at` must fall inside the
    # current parole generation (>= parole_at) — the retest cohort is anchored
    # on the generation, so trades predating it are not retest evidence.
    # Exactly FEEDBACK_PAROLE_RETEST_TRADES admissions: a 5-slot parole cannot
    # produce more, and a larger cohort now trips the bypass-contamination check.
    for _ in range(s.FEEDBACK_PAROLE_RETEST_TRADES):
        await _insert_trade(
            db,
            "recovered",
            10,
            5.0,
            now - timedelta(hours=1),
            opened_at=now - timedelta(hours=12),
        )
    await combo_refresh.refresh_combo(db, "recovered", s)
    row = await _get_combo_row(db, "recovered", "30d")
    # Retest COMPLETE (15 >= 5 resolved) and wr >= 30: clear suppression.
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
    # Recent trades still poor. `opened_at` inside the current parole
    # generation so they count as resolved retest outcomes.
    for _ in range(s.FEEDBACK_PAROLE_RETEST_TRADES):
        await _insert_trade(
            db,
            "re_supp",
            -5,
            -3,
            now - timedelta(hours=1),
            opened_at=now - timedelta(hours=12),
        )
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


async def test_closed_manual_excluded_from_wr(tmp_path, settings_factory):
    """closed_manual trades must NOT count in win-rate aggregation.

    Pre-fix: 10 closed_tp wins + 10 closed_manual (0 pnl) → WR=50% (50% diluted).
    Post-fix: only closed_tp / closed_sl / closed_expired count → WR=100%.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    combo = "manual_test_combo"

    # 10 proper TP wins.
    for _ in range(10):
        await _insert_trade(
            db, combo, 10.0, 5.0, now - timedelta(days=2), status="closed_tp"
        )
    # 10 force-close manual exits (zero pnl_usd — counts as a loss in naïve query).
    for _ in range(10):
        await _insert_trade(
            db, combo, 0.0, 0.0, now - timedelta(days=2), status="closed_manual"
        )

    ok = await combo_refresh.refresh_combo(db, combo, s)
    assert ok is True

    row = await _get_combo_row(db, combo, "30d")
    # Only the 10 closed_tp rows should count.
    assert (
        row["trades"] == 10
    ), f"Expected 10 trades (closed_manual excluded), got {row['trades']}"
    assert (
        abs(row["win_rate_pct"] - 100.0) < 0.01
    ), f"Expected 100% WR (all closed_tp wins), got {row['win_rate_pct']}"
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
    await db.close()


async def test_chronic_failure_threshold_detected(
    tmp_path, settings_factory, monkeypatch
):
    """refresh_all returns combos whose 30d window refresh_failures >= threshold.

    The combo must actually FAIL its refresh on this run. Before
    fix/refresh-enumeration-closed-window this fixture seeded a counter on a row
    that was never enumerated at all, so nothing could reset it. Now every
    existing 30d key IS enumerated, and a SUCCESSFUL refresh zeroes the counter
    via the UPSERT — so a never-refreshed sentinel can no longer stand in for a
    chronically failing combo. Forces a real failure through the established
    `db._conn.execute` seam (see test_failure_counter_scoped_to_window) so the
    genuine increment path runs.
    """
    import aiosqlite

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # One short of the threshold; THIS run's failure is what crosses it.
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES ('stuck', '30d', 0, 0, 0, 0, 0, 0, 0, ?, ?)",
        (s.FEEDBACK_CHRONIC_FAILURE_THRESHOLD - 1, now.isoformat()),
    )
    await db._conn.commit()

    original_execute = db._conn.execute

    async def _fail_7d_upsert(sql, *args, **kwargs):
        if "INSERT INTO combo_performance" in str(sql) and "'7d'" in str(sql):
            raise aiosqlite.OperationalError("forced refresh failure")
        return await original_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db._conn, "execute", _fail_7d_upsert)
    summary = await combo_refresh.refresh_all(db, s)
    monkeypatch.setattr(db._conn, "execute", original_execute)

    assert summary["failed"] >= 1, "fixture invalid: the refresh did not fail"
    assert "stuck" in summary["chronic_failures"]
    await db.close()


async def test_failure_counter_scoped_to_window(
    tmp_path, settings_factory, monkeypatch
):
    """A single refresh_combo failure must only increment the 30d row, not the 7d row."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    combo = "scope_test"

    # Pre-seed both windows with refresh_failures=0.
    for window in ("7d", "30d"):
        await db._conn.execute(
            "INSERT INTO combo_performance "
            "(combo_key, window, trades, wins, losses, total_pnl_usd, "
            " avg_pnl_pct, win_rate_pct, suppressed, refresh_failures, last_refreshed) "
            "VALUES (?, ?, 0, 0, 0, 0, 0, 0, 0, 0, ?)",
            (combo, window, now.isoformat()),
        )
    await db._conn.commit()

    # Seed one closed_tp trade so the SELECT query runs.
    await _insert_trade(db, combo, 10.0, 5.0, now - timedelta(days=1))

    original_execute = db._conn.execute
    import aiosqlite

    async def _fail_on_upsert(sql, *args, **kwargs):
        if "INSERT INTO combo_performance" in str(sql) and "'7d'" in str(sql):
            raise aiosqlite.OperationalError("forced 7d upsert failure")
        return await original_execute(sql, *args, **kwargs)

    monkeypatch.setattr(db._conn, "execute", _fail_on_upsert)
    ok = await combo_refresh.refresh_combo(db, combo, s)
    assert ok is False

    monkeypatch.setattr(db._conn, "execute", original_execute)

    # 30d window: refresh_failures must have incremented.
    cur = await db._conn.execute(
        "SELECT refresh_failures FROM combo_performance WHERE combo_key=? AND window='30d'",
        (combo,),
    )
    row_30d = await cur.fetchone()
    assert (
        row_30d["refresh_failures"] >= 1
    ), "30d refresh_failures must increment on error"

    # 7d window: refresh_failures must still be 0 (scoped update).
    cur = await db._conn.execute(
        "SELECT refresh_failures FROM combo_performance WHERE combo_key=? AND window='7d'",
        (combo,),
    )
    row_7d = await cur.fetchone()
    assert (
        row_7d["refresh_failures"] == 0
    ), f"7d refresh_failures must stay at 0, got {row_7d['refresh_failures']}"
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
    assert (
        row["parole_at"] == original_parole
    ), f"parole_at was overwritten: expected {original_parole!r}, got {row['parole_at']!r}"
    await db.close()


async def test_refresh_counts_closed_trailing_stop_in_rollup(
    tmp_path, settings_factory
):
    """closed_trailing_stop must be included in 7d/30d trade counts and win-rate.

    Trailing stops book profit by design; excluding them from the feedback loop
    would make combos that benefit most from trailing look worse in stats and
    trigger spurious suppression.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)

    # 2 trailing-stop wins + 1 TP win + 1 SL loss = 4 trades, 3 wins
    await _insert_trade(
        db,
        "trail_combo",
        15.0,
        10.0,
        now - timedelta(days=1),
        status="closed_trailing_stop",
    )
    await _insert_trade(
        db,
        "trail_combo",
        12.0,
        8.0,
        now - timedelta(days=1),
        status="closed_trailing_stop",
    )
    await _insert_trade(
        db, "trail_combo", 20.0, 15.0, now - timedelta(days=1), status="closed_tp"
    )
    await _insert_trade(
        db, "trail_combo", -10.0, -8.0, now - timedelta(days=1), status="closed_sl"
    )

    ok = await combo_refresh.refresh_combo(db, "trail_combo", s)
    assert ok

    row = await _get_combo_row(db, "trail_combo", "7d")
    assert row["trades"] == 4
    assert row["wins"] == 3
    assert row["losses"] == 1
    assert abs(row["win_rate_pct"] - 75.0) < 0.01
    await db.close()


async def test_refresh_counts_closed_moonshot_trail_in_rollup(
    tmp_path, settings_factory
):
    """closed_moonshot_trail (BL-063) must be counted in 7d/30d rollups
    just like closed_trailing_stop. Locks in the CLOSED_COUNTABLE_STATUSES
    contract so a future refactor can't silently exclude moonshot exits
    from combo_performance.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)

    # 2 moonshot-trail wins + 1 TP win + 1 SL loss = 4 trades, 3 wins
    await _insert_trade(
        db,
        "moon_combo",
        45.0,
        30.0,
        now - timedelta(days=1),
        status="closed_moonshot_trail",
    )
    await _insert_trade(
        db,
        "moon_combo",
        80.0,
        60.0,
        now - timedelta(days=1),
        status="closed_moonshot_trail",
    )
    await _insert_trade(
        db,
        "moon_combo",
        20.0,
        15.0,
        now - timedelta(days=1),
        status="closed_tp",
    )
    await _insert_trade(
        db,
        "moon_combo",
        -10.0,
        -8.0,
        now - timedelta(days=1),
        status="closed_sl",
    )

    ok = await combo_refresh.refresh_combo(db, "moon_combo", s)
    assert ok

    row = await _get_combo_row(db, "moon_combo", "7d")
    assert row["trades"] == 4
    assert row["wins"] == 3
    assert row["losses"] == 1
    assert abs(row["win_rate_pct"] - 75.0) < 0.01
    await db.close()


async def test_refresh_counts_tg_social_signal_type_in_rollup(
    tmp_path, settings_factory
):
    """BL-064 regression: tg_social signal_type contributes to combo_performance
    rollups across all CLOSED_COUNTABLE_STATUSES — including closed_moonshot_trail
    which BL-063 added. Locks the contract that a future refactor of
    CLOSED_COUNTABLE_STATUSES doesn't silently exclude tg_social."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)

    for status, pct in [
        ("closed_tp", 25.0),
        ("closed_trailing_stop", 12.0),
        ("closed_moonshot_trail", 60.0),
        ("closed_expired", -3.0),
        ("closed_sl", -15.0),
    ]:
        await _insert_trade(
            db,
            "tg_social",
            pct,
            pct,
            now - timedelta(days=1),
            status=status,
        )

    ok = await combo_refresh.refresh_combo(db, "tg_social", s)
    assert ok
    row = await _get_combo_row(db, "tg_social", "7d")
    assert row["trades"] == 5
    # Wins: tp(+25), trail(+12), moonshot(+60) = 3 wins; expired(-3) and sl(-15) = 2 losses
    assert row["wins"] == 3
    assert row["losses"] == 2
    await db.close()


# ---------------------------------------------------------------------------
# fix/frozen-suppression-lock — refresh suppressed zero-trade combos so a
# suppressed signal cannot latch at parole_exhausted forever, silently, only
# because it fell out of the trade-only 30d refresh window (funnel iv). Plus a
# §12b operator alert on entry into that permanent-suppression state.
# ---------------------------------------------------------------------------


class _StubSender:
    """Records permanent-suppression alert sends without importing aiohttp.

    A module-level `import aiohttp` aborts the interpreter on Windows dev boxes
    (OpenSSL Applink); the real `_send_permanent_suppression_alert` defers that
    import, and tests monkeypatch this stub in its place so it never runs.
    """

    def __init__(self):
        self.calls = 0
        self.messages: list[str] = []

    async def __call__(self, settings, message):
        self.calls += 1
        self.messages.append(message)


async def _seed_suppressed_combo(
    db,
    combo_key,
    *,
    remaining=0,
    parole_at=None,
    suppressed_at=None,
    last_refreshed=None,
    trades=25,
    wins=4,
    perm_alerted_at=None,
):
    """Insert a `combo_performance` 30d row already suppressed=1."""
    now = datetime.now(timezone.utc)
    suppressed_at = suppressed_at or (now - timedelta(days=20)).isoformat()
    parole_at = (
        parole_at if parole_at is not None else (now - timedelta(days=6)).isoformat()
    )
    last_refreshed = last_refreshed or (now - timedelta(days=1)).isoformat()
    losses = trades - wins
    wr = (100.0 * wins / trades) if trades else 0.0
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        " win_rate_pct, suppressed, suppressed_at, parole_at, "
        " parole_trades_remaining, refresh_failures, last_refreshed, "
        " perm_suppression_alerted_at) "
        "VALUES (?, '30d', ?, ?, ?, -100.0, -2.0, ?, 1, ?, ?, ?, 0, ?, ?)",
        (
            combo_key,
            trades,
            wins,
            losses,
            wr,
            suppressed_at,
            parole_at,
            remaining,
            last_refreshed,
            perm_alerted_at,
        ),
    )
    await db._conn.commit()


async def _scalar(db, sql, params=()):
    cur = await db._conn.execute(sql, params)
    row = await cur.fetchone()
    return row[0] if row else None


async def test_widened_refresh_refreshes_suppressed_zero_trade_combo(
    tmp_path, settings_factory, monkeypatch
):
    """(i) A suppressed combo with no trade in the 30d window IS now refreshed
    (was silently skipped by the trade-only selection before this fix)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    monkeypatch.setattr(
        combo_refresh, "_send_permanent_suppression_alert", _StubSender()
    )
    old_refreshed = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    # Seed with sentinel trades=25 + stale last_refreshed, and NO paper_trades.
    await _seed_suppressed_combo(
        db, "gainers_early", remaining=0, last_refreshed=old_refreshed, trades=25
    )
    await combo_refresh.refresh_all(db, s)

    # Recomputed → sentinel trades=25 collapsed to 0-in-window and
    # last_refreshed advanced. Under the OLD query neither would change.
    new_refreshed = await _scalar(
        db,
        "SELECT last_refreshed FROM combo_performance "
        "WHERE combo_key='gainers_early' AND window='30d'",
    )
    assert new_refreshed != old_refreshed, "suppressed zero-trade combo was skipped"
    row = await _get_combo_row(db, "gainers_early", "30d")
    assert row["trades"] == 0
    await db.close()


async def test_widened_refresh_keeps_suppressed_no_auto_unlatch(
    tmp_path, settings_factory, monkeypatch
):
    """(ii) constraint (a): a suppressed losing combo STAYS suppressed after the
    widened refresh — no auto-unlatch and no parole reset (no auto-revival)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    monkeypatch.setattr(
        combo_refresh, "_send_permanent_suppression_alert", _StubSender()
    )
    original_parole = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    # parole_exhausted (remaining=0) — the exact frozen state of the two combos
    # already permanently locked (gainers_early, losers_contrarian).
    await _seed_suppressed_combo(
        db, "losers_contrarian", remaining=0, parole_at=original_parole
    )
    await combo_refresh.refresh_all(db, s)

    row = await _get_combo_row(db, "losers_contrarian", "30d")
    assert row["suppressed"] == 1, "must stay suppressed"
    # NOT reset to FEEDBACK_PAROLE_RETEST_TRADES (5) — that would be auto-revival.
    assert row["parole_trades_remaining"] == 0
    assert row["parole_at"] == original_parole, "parole_at must not be reset"
    await db.close()


async def test_permanent_suppression_alert_fires_once_deduped(
    tmp_path, settings_factory, monkeypatch
):
    """(iii) The §12b permanent-suppression alert fires once and is deduped on
    the second run (marker set, no re-alert)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    stub = _StubSender()
    monkeypatch.setattr(combo_refresh, "_send_permanent_suppression_alert", stub)
    await _seed_suppressed_combo(db, "losers_contrarian", remaining=0)

    summary1 = await combo_refresh.refresh_all(db, s)
    assert stub.calls == 1
    assert "losers_contrarian" in summary1["permanent_suppression"]
    # J1: the body no longer claims permanence. Assert what it must now SAY —
    # the state, and both gates with their distinct remedies — rather than the
    # old phrase, so this test tracks meaning instead of prose.
    body = stub.messages[0]
    assert "SUPPRESSED AND IDLE" in body
    assert "not permanent" in body.lower()
    assert "combo_performance.suppressed" in body
    assert "signal_params.enabled" in body
    assert "revive_signal_with_baseline" in stub.messages[0]
    marker = await _scalar(
        db,
        "SELECT perm_suppression_alerted_at FROM combo_performance "
        "WHERE combo_key='losers_contrarian' AND window='30d'",
    )
    assert marker is not None, "dedup marker must be set after a confirmed send"

    # Second run — still in state, marker set → deduped, no re-alert.
    summary2 = await combo_refresh.refresh_all(db, s)
    assert stub.calls == 1, "must NOT re-alert on the second run"
    assert summary2["permanent_suppression"] == []
    await db.close()


async def test_permanent_suppression_alert_rearms_after_leaving_state(
    tmp_path, settings_factory, monkeypatch
):
    """Dedup marker re-arms: if a combo leaves the permanent-suppression state
    (here: a fresh in-window trade) the marker clears so a future re-entry
    alerts again."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    stub = _StubSender()
    monkeypatch.setattr(combo_refresh, "_send_permanent_suppression_alert", stub)
    # Run 2 re-suppresses on the fresh losing trade (a §12b reversal) — stub the
    # reversal sender so it doesn't reach the real aiohttp send path.
    monkeypatch.setattr(
        combo_refresh, "_send_suppression_reversal_alert", _StubSender()
    )
    await _seed_suppressed_combo(db, "gainers_early", remaining=0)

    await combo_refresh.refresh_all(db, s)
    assert stub.calls == 1

    # Combo trades again inside the window → leaves permanent-suppression state.
    now = datetime.now(timezone.utc)
    await _insert_trade(db, "gainers_early", -5, -3.0, now - timedelta(days=1))
    await combo_refresh.refresh_all(db, s)
    marker = await _scalar(
        db,
        "SELECT perm_suppression_alerted_at FROM combo_performance "
        "WHERE combo_key='gainers_early' AND window='30d'",
    )
    assert marker is None, "marker must re-arm once the combo leaves the state"
    await db.close()


async def test_permanent_suppression_alert_failure_does_not_break_refresh(
    tmp_path, settings_factory, monkeypatch
):
    """(iv) An alert delivery failure never breaks refresh, and leaves the dedup
    marker NULL so the next run re-attempts (operator MUST eventually be told)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()

    async def _boom(settings, message):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(combo_refresh, "_send_permanent_suppression_alert", _boom)
    await _seed_suppressed_combo(db, "gainers_early", remaining=0)

    summary = await combo_refresh.refresh_all(db, s)  # must NOT raise
    assert summary["failed"] == 0
    # Combo still refreshed + suppressed despite the alert failure.
    row = await _get_combo_row(db, "gainers_early", "30d")
    assert row["suppressed"] == 1
    # Marker NOT set → retried next run.
    marker = await _scalar(
        db,
        "SELECT perm_suppression_alerted_at FROM combo_performance "
        "WHERE combo_key='gainers_early' AND window='30d'",
    )
    assert marker is None
    # Not counted as newly-alerted this run.
    assert summary["permanent_suppression"] == []
    await db.close()


async def test_normal_traded_combo_refresh_unchanged(
    tmp_path, settings_factory, monkeypatch
):
    """(v) A normal combo that traded inside the window refreshes exactly as
    before and is never flagged as permanent-suppression."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    monkeypatch.setattr(
        combo_refresh, "_send_permanent_suppression_alert", _StubSender()
    )
    now = datetime.now(timezone.utc)
    for pnl in [10, 20, 30]:
        await _insert_trade(db, "healthy", pnl, 5.0, now - timedelta(days=1))

    summary = await combo_refresh.refresh_all(db, s)
    row = await _get_combo_row(db, "healthy", "30d")
    assert row["trades"] == 3
    assert row["suppressed"] == 0
    assert row["parole_at"] is None
    assert "healthy" not in summary["permanent_suppression"]
    await db.close()


async def test_unsuppressed_zero_trade_combo_is_now_refreshed(
    tmp_path, settings_factory, monkeypatch
):
    """(vi) SUPERSEDED AND INVERTED by fix/refresh-enumeration-closed-window.

    This test previously asserted the OPPOSITE — "an UNSUPPRESSED combo with no
    recent trade is NOT force-refreshed; only suppressed combos get the
    widening." That contract is exactly what froze seven unsuppressed 30d rows
    with stale economics (some since May 2026): a row whose outcomes aged out of
    the window could never be corrected downward, because nothing re-selected
    it. The operator ruling of 2026-08-13 replaces it with the enumeration
    invariant, under which this sentinel row MUST age to current-window
    economics.

    Kept in place rather than deleted so the inversion is explicit to a
    reviewer; the general case is covered by
    test_stale_unsuppressed_row_ages_down_to_current_window.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    monkeypatch.setattr(
        combo_refresh, "_send_permanent_suppression_alert", _StubSender()
    )
    old_refreshed = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    # Unsuppressed sentinel row (trades=7), no paper_trades in window.
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        " win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES ('quiet', '30d', 7, 5, 2, 50, 5, 71.4, 0, 0, ?)",
        (old_refreshed,),
    )
    await db._conn.commit()

    summary = await combo_refresh.refresh_all(db, s)
    # The sentinel is now corrected to the truth: zero outcomes in the window.
    row = await _get_combo_row(db, "quiet", "30d")
    assert row["trades"] == 0, "stale sentinel economics survived the refresh"
    assert row["wins"] == 0
    assert row["losses"] == 0
    new_refreshed = await _scalar(
        db,
        "SELECT last_refreshed FROM combo_performance "
        "WHERE combo_key='quiet' AND window='30d'",
    )
    assert new_refreshed != old_refreshed, "row was not actually re-refreshed"
    # Still not a permanent-suppression event — it is unsuppressed.
    assert "quiet" not in summary["permanent_suppression"]
    assert row["suppressed"] == 0
    await db.close()


async def test_chain_completed_frozen_lock_regression(
    tmp_path, settings_factory, monkeypatch
):
    """Real-world regression fixture: chain_completed frozen-lock snapshot
    captured 2026-07-03 — see
    tests/fixtures/frozen_lock_chain_completed_snapshot.md.

    chain_completed was suppressed 2026-06-19, last_open 2026-06-04,
    parole_trades_remaining 5, 63 trades / 4 wins (6.35% WR). At the 2026-07-04
    03:00Z refresh its last_open drops outside the 30d window; under the OLD
    trade-only refresh set it would fall out of refresh and latch silently at
    parole_exhausted forever. This test simulates the post-latch state (last
    trade outside the window) and asserts the fix keeps it live + suppressed
    with NO auto-revival (constraint a) and alerts the operator once (§12b)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    stub = _StubSender()
    monkeypatch.setattr(combo_refresh, "_send_permanent_suppression_alert", stub)
    now = datetime.now(timezone.utc)

    # One historical trade opened + closed just OUTSIDE the 30d window.
    await _insert_trade(
        db,
        "chain_completed",
        -10.0,
        -8.0,
        now - timedelta(days=31),
        status="closed_sl",
        opened_at=now - timedelta(days=31, hours=1),
    )
    # Fixture snapshot row: suppressed, remaining=5, 63 trades / 4 wins.
    await _seed_suppressed_combo(
        db,
        "chain_completed",
        remaining=5,
        trades=63,
        wins=4,
        parole_at=now.isoformat(),
    )

    summary = await combo_refresh.refresh_all(db, s)
    row = await _get_combo_row(db, "chain_completed", "30d")
    # Kept live: refreshed → trades recomputed to 0-in-window.
    assert row["trades"] == 0
    # constraint (a): STAYS suppressed, parole allowance NOT reset.
    assert row["suppressed"] == 1
    assert row["parole_trades_remaining"] == 5
    # §12b: operator alerted exactly once.
    assert stub.calls == 1
    assert "chain_completed" in summary["permanent_suppression"]
    await db.close()


# ---------------------------------------------------------------------------
# SIG-07 residual — §12b operator alerts at the combo-suppression WRITE sites
# that reverse operator-favorable (active/unsuppressed) state. Two transitions:
#   * newly_suppressed             — an unsuppressed combo becomes suppressed=1
#   * parole_exhausted_resuppressed — a paroled combo fails its retest on real
#                                     trades and re-latches with a fresh parole
# Both silently darkened gainers_early (combo-suppressed 2026-06-12, unnoticed
# 7.5 weeks). #424 covers only the aged-out permanent state; these cover the
# transition itself.
# ---------------------------------------------------------------------------


async def test_newly_suppressed_combo_fires_reversal_alert(
    tmp_path, settings_factory, monkeypatch
):
    """An active (unsuppressed) combo that crosses the suppression rule fires a
    §12b reversal alert naming the combo, the trigger stats, and the revival
    command, with dispatched + delivered trace logs."""
    import structlog

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    perm_stub = _StubSender()
    rev_stub = _StubSender()
    monkeypatch.setattr(combo_refresh, "_send_permanent_suppression_alert", perm_stub)
    monkeypatch.setattr(combo_refresh, "_send_suppression_reversal_alert", rev_stub)

    now = datetime.now(timezone.utc)
    # Fresh combo: 5 wins + 15 losses = 20 trades → 25% WR (< 30% threshold).
    for _ in range(5):
        await _insert_trade(db, "gainers_early", 10, 5.0, now - timedelta(days=2))
    for _ in range(15):
        await _insert_trade(db, "gainers_early", -5, -3.0, now - timedelta(days=2))

    with structlog.testing.capture_logs() as log_events:
        summary = await combo_refresh.refresh_all(db, s)

    row = await _get_combo_row(db, "gainers_early", "30d")
    assert row["suppressed"] == 1

    # Exactly one reversal alert, with combo + stats + revival command.
    assert rev_stub.calls == 1
    msg = rev_stub.messages[0]
    assert "gainers_early" in msg
    assert "revive_signal_with_baseline" in msg
    assert "25.0%" in msg
    assert "n=20" in msg

    # Surfaced in the summary for main.py logging.
    reversals = summary["suppression_reversals"]
    assert any(
        r["combo_key"] == "gainers_early" and r["transition"] == "newly_suppressed"
        for r in reversals
    )

    # §12b dispatched + delivered trace pair.
    events = {e["event"] for e in log_events}
    assert "suppression_reversal_alert_dispatched" in events
    assert "suppression_reversal_alert_delivered" in events

    # Not permanent-suppression — it just traded inside the window.
    assert perm_stub.calls == 0
    await db.close()


async def test_newly_suppressed_reversal_not_realerted_second_run(
    tmp_path, settings_factory, monkeypatch
):
    """The reversal alert fires once on the transition and is naturally deduped:
    a subsequent refresh sees the combo already suppressed (no transition)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    monkeypatch.setattr(
        combo_refresh, "_send_permanent_suppression_alert", _StubSender()
    )
    rev_stub = _StubSender()
    monkeypatch.setattr(combo_refresh, "_send_suppression_reversal_alert", rev_stub)

    now = datetime.now(timezone.utc)
    for _ in range(5):
        await _insert_trade(db, "gainers_early", 10, 5.0, now - timedelta(days=2))
    for _ in range(15):
        await _insert_trade(db, "gainers_early", -5, -3.0, now - timedelta(days=2))

    await combo_refresh.refresh_all(db, s)
    assert rev_stub.calls == 1

    summary2 = await combo_refresh.refresh_all(db, s)
    assert rev_stub.calls == 1, "must NOT re-alert while state is unchanged"
    assert summary2["suppression_reversals"] == []
    await db.close()


async def test_parole_exhausted_resuppression_fires_reversal_alert(
    tmp_path, settings_factory, monkeypatch
):
    """A suppressed combo whose parole is exhausted, retested on real trades and
    failed, re-latches with a fresh parole window — a §12b 'failed parole retest'
    reversal alert must fire."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    perm_stub = _StubSender()
    rev_stub = _StubSender()
    monkeypatch.setattr(combo_refresh, "_send_permanent_suppression_alert", perm_stub)
    monkeypatch.setattr(combo_refresh, "_send_suppression_reversal_alert", rev_stub)

    now = datetime.now(timezone.utc)
    original_parole = (now - timedelta(days=6)).isoformat()  # window already open
    await _seed_suppressed_combo(
        db, "gainers_early", remaining=0, parole_at=original_parole
    )
    # Real, in-window retest trades that fail (0% WR) — NOT a zero-trade combo.
    for _ in range(s.FEEDBACK_PAROLE_RETEST_TRADES):
        await _insert_trade(
            db,
            "gainers_early",
            -5,
            -3.0,
            now - timedelta(days=2),
            opened_at=now - timedelta(days=5),
        )

    summary = await combo_refresh.refresh_all(db, s)

    row = await _get_combo_row(db, "gainers_early", "30d")
    assert row["suppressed"] == 1
    # Re-armed with a fresh parole window (the transition marker).
    assert row["parole_trades_remaining"] == s.FEEDBACK_PAROLE_RETEST_TRADES
    assert row["parole_at"] != original_parole

    assert rev_stub.calls == 1
    msg = rev_stub.messages[0]
    assert "gainers_early" in msg
    assert "parole" in msg.lower()
    assert "revive_signal_with_baseline" in msg
    assert any(
        r["transition"] == "parole_exhausted_resuppressed"
        for r in summary["suppression_reversals"]
    )
    # It traded inside the window → not a permanent-suppression event.
    assert perm_stub.calls == 0
    await db.close()


async def test_reversal_alert_failure_does_not_break_refresh(
    tmp_path, settings_factory, monkeypatch
):
    """A reversal-alert delivery failure never breaks refresh, and the combo is
    NOT counted as alerted (so a future run can re-attempt if still in state)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    monkeypatch.setattr(
        combo_refresh, "_send_permanent_suppression_alert", _StubSender()
    )

    async def _boom(settings, message):
        raise RuntimeError("telegram down")

    monkeypatch.setattr(combo_refresh, "_send_suppression_reversal_alert", _boom)

    now = datetime.now(timezone.utc)
    for _ in range(5):
        await _insert_trade(db, "gainers_early", 10, 5.0, now - timedelta(days=2))
    for _ in range(15):
        await _insert_trade(db, "gainers_early", -5, -3.0, now - timedelta(days=2))

    summary = await combo_refresh.refresh_all(db, s)  # must NOT raise
    assert summary["failed"] == 0
    row = await _get_combo_row(db, "gainers_early", "30d")
    assert row["suppressed"] == 1
    assert summary["suppression_reversals"] == []
    await db.close()


async def test_healthy_combo_no_reversal_alert(tmp_path, settings_factory, monkeypatch):
    """A profitable combo that never suppresses produces no reversal alert."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    rev_stub = _StubSender()
    monkeypatch.setattr(combo_refresh, "_send_suppression_reversal_alert", rev_stub)
    monkeypatch.setattr(
        combo_refresh, "_send_permanent_suppression_alert", _StubSender()
    )
    now = datetime.now(timezone.utc)
    for pnl in [10, 20, 30]:
        await _insert_trade(db, "healthy", pnl, 5.0, now - timedelta(days=1))

    summary = await combo_refresh.refresh_all(db, s)
    assert rev_stub.calls == 0
    assert summary["suppression_reversals"] == []
    await db.close()


async def test_preserve_suppressed_combo_no_reversal_alert(
    tmp_path, settings_factory, monkeypatch
):
    """A zero-trade suppressed combo (preserve branch) is NOT a reversal — it is
    the permanent-suppression path (#424). Only the perm alert fires."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    perm_stub = _StubSender()
    rev_stub = _StubSender()
    monkeypatch.setattr(combo_refresh, "_send_permanent_suppression_alert", perm_stub)
    monkeypatch.setattr(combo_refresh, "_send_suppression_reversal_alert", rev_stub)

    await _seed_suppressed_combo(db, "gainers_early", remaining=0)

    summary = await combo_refresh.refresh_all(db, s)
    assert rev_stub.calls == 0
    assert perm_stub.calls == 1
    assert summary["suppression_reversals"] == []
    await db.close()


def test_reversal_alert_sender_uses_plain_text_and_source():
    """§12b: the reversal sender must pass parse_mode=None (underscore-laden
    combo/signal names + revive_signal_with_baseline would mangle under
    MarkdownV1) and tag a source= for callsite traceability."""
    import inspect

    src = inspect.getsource(combo_refresh._send_suppression_reversal_alert)
    assert "parse_mode=None" in src
    assert "source=" in src


# ---------------------------------------------------------------------------
# fix/suppression-alert-delivery-failure — the two §12b suppression senders
# inherited the pre-#523 pattern: they called the alerter WITHOUT
# `raise_on_failure=True`. The alerter defaults to False and merely LOGS a
# non-200, so a rejected page returned normally, the caller logged
# `..._delivered` and stamped its alerted marker — silencing the operator
# notification forever. These tests drive the REAL alerter (aioresponses fails
# the actual Telegram POST); mocking the sender itself would mock away the very
# seam that was broken.
# ---------------------------------------------------------------------------


def _tg_url(settings) -> str:
    return f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"


def _reset_tg_module_state() -> None:
    """Clear the process-global pacing deadline + burst counter.

    `TG_PACING_ENABLED` defaults True, so a 429 registered by an earlier test
    for the same chat id would make these tests sleep before sending.
    """
    from scout.observability import tg_dispatch_counter, tg_pacing

    tg_pacing.reset_for_tests()
    tg_dispatch_counter.reset_for_tests()


async def _seed_newly_suppressing_trades(db):
    """5 wins + 15 losses = 25% WR (< 30% threshold) -> `newly_suppressed`."""
    now = datetime.now(timezone.utc)
    for _ in range(5):
        await _insert_trade(db, "gainers_early", 10, 5.0, now - timedelta(days=2))
    for _ in range(15):
        await _insert_trade(db, "gainers_early", -5, -3.0, now - timedelta(days=2))


async def _perm_marker(db, combo_key):
    """Return `(row_exists, perm_suppression_alerted_at)` for the 30d row.

    Returns the row's existence separately so a caller can assert the row is
    THERE before asserting on the column — a missing row would otherwise make
    an `is None` marker assertion pass vacuously.
    """
    cur = await db._conn.execute(
        "SELECT perm_suppression_alerted_at FROM combo_performance "
        "WHERE combo_key = ? AND window = '30d'",
        (combo_key,),
    )
    row = await cur.fetchone()
    return row is not None, (row[0] if row else None)


async def test_reversal_sender_opts_into_raise_on_failure(settings_factory):
    """(3a) A rejected page must propagate OUT of the reversal sender.

    Contract-tests the real `scout.alerter` seam, not the monkeypatched wrapper
    the other tests use: without `raise_on_failure=True` the alerter swallows a
    non-200 and returns normally, so the caller's `except` never runs.
    """
    import scout.alerter as alerter_mod

    captured: dict = {}

    async def fake_send(message, session, settings, **kw):
        captured.update(kw)
        raise RuntimeError("telegram 400")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(alerter_mod, "send_telegram_message", fake_send)
        with pytest.raises(RuntimeError):
            await combo_refresh._send_suppression_reversal_alert(
                settings_factory(), "body"
            )

    assert captured.get("raise_on_failure") is True, "failure cannot propagate"
    assert captured.get("parse_mode") is None, "combo names contain underscores"
    assert captured.get("source") == "combo_refresh_suppression_reversal"


async def test_permanent_sender_opts_into_raise_on_failure(settings_factory):
    """(3a) Same contract for the permanent-suppression sender."""
    import scout.alerter as alerter_mod

    captured: dict = {}

    async def fake_send(message, session, settings, **kw):
        captured.update(kw)
        raise RuntimeError("telegram 400")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(alerter_mod, "send_telegram_message", fake_send)
        with pytest.raises(RuntimeError):
            await combo_refresh._send_permanent_suppression_alert(
                settings_factory(), "body"
            )

    assert captured.get("raise_on_failure") is True, "failure cannot propagate"
    assert captured.get("parse_mode") is None, "signal names contain underscores"
    assert captured.get("source") == "combo_refresh_permanent_suppression"


async def test_reversal_not_recorded_when_telegram_rejects_the_page(
    tmp_path, settings_factory
):
    """(3b) Telegram 500 -> the combo is NOT recorded as alerted.

    The whole alerter runs for real; only the HTTP layer fails. Before the fix
    the 500 was swallowed, `..._delivered` was logged and the combo was appended
    to the reversal list as if the operator had been told.
    """
    import structlog
    from aioresponses import aioresponses

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_newly_suppressing_trades(db)

    with aioresponses() as m:
        m.post(_tg_url(s), status=500, body="rejected", repeat=True)
        with structlog.testing.capture_logs() as log_events:
            summary = await combo_refresh.refresh_all(db, s)  # must NOT raise

    # The transition really happened — so an empty list below means "not
    # alerted", not "nothing to alert about".
    row = await _get_combo_row(db, "gainers_early", "30d")
    assert row is not None, "30d row missing — the marker assertion would be vacuous"
    assert row["suppressed"] == 1

    assert "suppression_reversals" in summary, "summary key vanished"
    assert summary["suppression_reversals"] == [], "combo marked alerted on a 500"

    events = [e["event"] for e in log_events]
    assert events.count("suppression_reversal_alert_dispatched") == 1
    assert events.count("suppression_reversal_alert_failed") == 1
    assert (
        "suppression_reversal_alert_delivered" not in events
    ), "logged delivered for a page Telegram rejected"
    await db.close()


async def test_reversal_recorded_and_logged_when_telegram_accepts(
    tmp_path, settings_factory
):
    """(3c + 3d) Telegram 200 -> recorded as alerted, with the §12b log pair."""
    import structlog
    from aioresponses import aioresponses

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_newly_suppressing_trades(db)

    with aioresponses() as m:
        m.post(_tg_url(s), status=200, payload={"ok": True}, repeat=True)
        with structlog.testing.capture_logs() as log_events:
            summary = await combo_refresh.refresh_all(db, s)

    assert summary["suppression_reversals"] == [
        {"combo_key": "gainers_early", "transition": "newly_suppressed"}
    ]

    events = [e["event"] for e in log_events]
    assert events.count("suppression_reversal_alert_dispatched") == 1
    assert events.count("suppression_reversal_alert_delivered") == 1
    assert "suppression_reversal_alert_failed" not in events
    await db.close()


async def test_perm_marker_not_stamped_when_telegram_rejects_the_page(
    tmp_path, settings_factory
):
    """(3b) Telegram 500 -> `perm_suppression_alerted_at` stays NULL.

    This is the durable one: a stamped marker dedups the alert forever, so a
    swallowed 500 meant the operator was never told a signal had entered
    permanent suppression.
    """
    import structlog
    from aioresponses import aioresponses

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_suppressed_combo(db, "gainers_early", remaining=0)

    with aioresponses() as m:
        m.post(_tg_url(s), status=500, body="rejected", repeat=True)
        with structlog.testing.capture_logs() as log_events:
            summary = await combo_refresh.refresh_all(db, s)  # must NOT raise

    exists, marker = await _perm_marker(db, "gainers_early")
    assert exists, "30d row missing — the marker assertion would be vacuous"
    assert marker is None, "dedup marker stamped for a page Telegram rejected"
    assert summary["permanent_suppression"] == []

    events = [e["event"] for e in log_events]
    assert events.count("permanent_suppression_alert_dispatched") == 1
    assert events.count("permanent_suppression_alert_failed") == 1
    assert "permanent_suppression_alert_delivered" not in events

    # Un-stamped means the NEXT run re-attempts — the point of not stamping.
    with aioresponses() as m:
        m.post(_tg_url(s), status=200, payload={"ok": True}, repeat=True)
        summary2 = await combo_refresh.refresh_all(db, s)
    assert summary2["permanent_suppression"] == ["gainers_early"]
    await db.close()


async def test_perm_marker_stamped_and_logged_when_telegram_accepts(
    tmp_path, settings_factory
):
    """(3c + 3d) Telegram 200 -> marker stamped, with the §12b log pair."""
    import structlog
    from aioresponses import aioresponses

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_suppressed_combo(db, "gainers_early", remaining=0)

    with aioresponses() as m:
        m.post(_tg_url(s), status=200, payload={"ok": True}, repeat=True)
        with structlog.testing.capture_logs() as log_events:
            summary = await combo_refresh.refresh_all(db, s)

    exists, marker = await _perm_marker(db, "gainers_early")
    assert exists, "30d row missing — the marker assertion would be vacuous"
    assert marker is not None, "confirmed send did not stamp the dedup marker"
    datetime.fromisoformat(marker)  # a real timestamp, not a truthy sentinel
    assert summary["permanent_suppression"] == ["gainers_early"]

    events = [e["event"] for e in log_events]
    assert events.count("permanent_suppression_alert_dispatched") == 1
    assert events.count("permanent_suppression_alert_delivered") == 1
    assert "permanent_suppression_alert_failed" not in events
    await db.close()


# ---------------------------------------------------------------------------
# fix/refresh-enumeration-closed-window — refresh_all enumerated candidates by
# `opened_at >= cutoff` while refresh_combo computes economics by
# `VALID_TERMINAL_OUTCOME_SQL AND closed_at >= cutoff`. Membership and
# economics were bound to DIFFERENT columns, so a combo whose opens aged out
# of the window while its closes stayed inside was never re-selected and its
# row froze. 2026-08-13: volume_spike had MAX(opened_at) 69 minutes before the
# cutoff, 21 valid closes inside the window, unsuppressed -> excluded entirely;
# seven unsuppressed 30d rows carried stale economics, some since May.
#
# The invariant now enumerated: every persisted combo whose 7d/30d economic
# state can change because outcomes entered OR aged out of the rolling window
# must be refreshed.
# ---------------------------------------------------------------------------


async def _insert_open_trade(db, combo_key: str, opened_at: datetime):
    """An OPEN trade: no closed_at, no pnl. Never a valid terminal outcome."""
    token_id = f"tok_{combo_key}_{next(_counter)}"
    await db._conn.execute(
        "INSERT INTO paper_trades "
        "(token_id, symbol, name, chain, signal_type, signal_data, "
        " entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price, "
        " status, opened_at, signal_combo) "
        "VALUES (?, 'S', 'N', 'coingecko', 'volume_spike', '{}', "
        " 1.0, 100.0, 100.0, 20.0, 10.0, 1.2, 0.9, 'open', ?, ?)",
        (token_id, opened_at.isoformat(), combo_key),
    )
    await db._conn.commit()


async def _insert_fallback_close(
    db, combo_key: str, closed_at: datetime, opened_at: datetime
):
    """A close EXCLUDED by provenance (`entry_fallback`, the fabricated-$0 class).

    Exercises the `exit_provenance` clause of VALID_TERMINAL_OUTCOME_SQL, which
    a `closed_manual` row alone would leave untested.
    """
    await _insert_trade(
        db, combo_key, 0.0, 0.0, closed_at, status="closed_expired", opened_at=opened_at
    )
    await db._conn.execute(
        "UPDATE paper_trades SET exit_provenance = 'entry_fallback' "
        "WHERE signal_combo = ? AND exit_provenance IS NULL",
        (combo_key,),
    )
    await db._conn.commit()


async def _seed_unsuppressed_combo_row(
    db, combo_key, *, trades=25, wins=5, last_refreshed=None
):
    """A stale, UNSUPPRESSED 30d row — the seven-row census class.

    Deliberately not `_seed_suppressed_combo`: a suppressed row is already
    enumerated by the legacy arm, so it cannot discriminate the new one.
    """
    now = datetime.now(timezone.utc)
    last_refreshed = last_refreshed or (now - timedelta(days=45)).isoformat()
    losses = trades - wins
    wr = (100.0 * wins / trades) if trades else 0.0
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        " win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES (?, '30d', ?, ?, ?, -100.0, -2.0, ?, 0, 0, ?)",
        (combo_key, trades, wins, losses, wr, last_refreshed),
    )
    await db._conn.commit()
    return last_refreshed


def _stub_suppression_senders(monkeypatch):
    """Both §12b senders stubbed — these tests are about ENUMERATION only."""
    monkeypatch.setattr(
        combo_refresh, "_send_permanent_suppression_alert", _StubSender()
    )
    monkeypatch.setattr(
        combo_refresh, "_send_suppression_reversal_alert", _StubSender()
    )


async def test_old_open_recent_valid_close_combo_is_refreshed(
    tmp_path, settings_factory, monkeypatch
):
    """(a) THE volume_spike CLASS: opened before the cutoff, valid close inside
    the window, unsuppressed, and NO pre-existing row.

    No pre-existing row is load-bearing: with one, the existing-keys arm would
    rescue this combo and the test could not discriminate the close arm.
    """
    from scout.timeutil import sql_utc_cutoff

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    _stub_suppression_senders(monkeypatch)

    now = datetime.now(timezone.utc)
    old_open = now - timedelta(days=45)  # BEFORE the 30d enumeration cutoff
    for pnl in (10.0, 20.0):
        await _insert_trade(
            db, "volume_spike", pnl, 5.0, now - timedelta(days=1), opened_at=old_open
        )
    await _insert_trade(
        db, "volume_spike", -5.0, -3.0, now - timedelta(days=1), opened_at=old_open
    )

    cur = await db._conn.execute(
        "SELECT MAX(opened_at) FROM paper_trades WHERE signal_combo = 'volume_spike'"
    )
    assert (await cur.fetchone())[0] < sql_utc_cutoff(
        days=s.FEEDBACK_REFRESH_WINDOW_DAYS
    ), "fixture invalid: opens must sit OUTSIDE the enumeration window"

    await combo_refresh.refresh_all(db, s)

    row = await _get_combo_row(db, "volume_spike", "30d")
    assert row is not None, "combo with in-window valid closes was never enumerated"
    assert row["trades"] == 3
    assert row["wins"] == 2
    assert row["losses"] == 1
    assert row["win_rate_pct"] == pytest.approx(200.0 / 3)
    await db.close()


async def test_stale_unsuppressed_row_ages_down_to_current_window(
    tmp_path, settings_factory, monkeypatch
):
    """(b) THE STALE-ROW CLASS: an existing unsuppressed 30d row with no recent
    opens AND no recent closes must be refreshed DOWN to current-window
    economics (ages to zero) with a fresh last_refreshed.

    Membership must not require any paper_trades activity at all — that is the
    whole point of the existing-keys arm.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    _stub_suppression_senders(monkeypatch)

    old_refreshed = await _seed_unsuppressed_combo_row(
        db, "stale_combo", trades=25, wins=5
    )
    # An ancient close: outside BOTH the enumeration window and 30d economics.
    ancient = datetime.now(timezone.utc) - timedelta(days=200)
    await _insert_trade(db, "stale_combo", 10.0, 5.0, ancient, opened_at=ancient)

    await combo_refresh.refresh_all(db, s)

    row = await _get_combo_row(db, "stale_combo", "30d")
    assert row is not None, "existing 30d row disappeared"
    assert row["trades"] == 0, "stale economics survived the refresh"
    assert row["wins"] == 0
    assert row["losses"] == 0
    assert row["win_rate_pct"] == 0.0
    assert row["suppressed"] == 0, "ageing to zero must not suppress"

    cur = await db._conn.execute(
        "SELECT last_refreshed FROM combo_performance "
        "WHERE combo_key = 'stale_combo' AND window = '30d'"
    )
    new_refreshed = (await cur.fetchone())[0]
    assert new_refreshed != old_refreshed, "row was not actually re-refreshed"
    await db.close()


async def test_recent_open_with_no_closes_still_enumerated(
    tmp_path, settings_factory, monkeypatch
):
    """(c) REGRESSION GUARD on the preserved opens arm: an open inside the
    window with no closes yet is still selected, and materializes a row.

    This is the cold-start / materialization protection the ruling keeps
    deliberately — the close arm cannot see it (nothing has closed) and the
    existing-keys arm cannot see it (no row yet).
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    _stub_suppression_senders(monkeypatch)

    await _insert_open_trade(
        db, "fresh_combo", datetime.now(timezone.utc) - timedelta(hours=2)
    )

    await combo_refresh.refresh_all(db, s)

    row = await _get_combo_row(db, "fresh_combo", "30d")
    assert row is not None, "recent-open combo lost its enumeration membership"
    assert row["trades"] == 0, "an open trade is not a terminal outcome"
    await db.close()


async def test_invalid_closes_do_not_create_membership_but_existing_row_ages(
    tmp_path, settings_factory, monkeypatch
):
    """(d) The close arm is bound to the CANONICAL validity predicate.

    First half: invalid-only closes (closed_manual + entry_fallback) for a combo
    with NO existing row and NO recent opens must NOT manufacture membership —
    otherwise the enumeration would resurrect exactly the fabricated-$0 and
    operator-action rows the economics predicate exists to exclude.

    Second half: the SAME invalid-only close pattern for a combo that DOES have
    a 30d row still participates, via the existing-keys arm, and ages correctly.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    _stub_suppression_senders(monkeypatch)

    now = datetime.now(timezone.utc)
    old_open = now - timedelta(days=45)
    recent_close = now - timedelta(days=1)

    # No row, no recent opens, only invalid closes -> must stay unknown.
    await _insert_trade(
        db,
        "invalid_only",
        0.0,
        0.0,
        recent_close,
        status="closed_manual",
        opened_at=old_open,
    )
    await _insert_fallback_close(db, "invalid_only", recent_close, old_open)

    # Same close shape, but a pre-existing 30d row -> enumerated via arm 3.
    await _seed_unsuppressed_combo_row(db, "invalid_with_row", trades=25, wins=5)
    await _insert_trade(
        db,
        "invalid_with_row",
        0.0,
        0.0,
        recent_close,
        status="closed_manual",
        opened_at=old_open,
    )

    await combo_refresh.refresh_all(db, s)

    assert (
        await _get_combo_row(db, "invalid_only", "30d")
    ) is None, "invalid-only closes manufactured enumeration membership"
    assert (
        await _get_combo_row(db, "invalid_only", "7d")
    ) is None, "invalid-only closes manufactured a 7d row"

    row = await _get_combo_row(db, "invalid_with_row", "30d")
    assert row is not None, "existing 30d row disappeared"
    assert row["trades"] == 0, "an invalid close must not count as economics"
    await db.close()


# ---------------------------------------------------------------------------
# fix/reversal-alert-durable-retry — the reversal page had no durable retry.
# A transition is diffed across ONE refresh, so once the combo is suppressed
# `_classify_reversal` returns None forever: a rejected page raised (#525),
# logged `_failed`, and was then permanently lost. The permanent-suppression
# path self-heals because its marker stays NULL until a confirmed send; the
# reversal path had no marker at all.
#
# Now the pending alert is persisted BEFORE delivery is attempted and cleared
# only after a confirmed send, so every later refresh re-attempts until it
# lands. Same "stamp only after a confirmed send" contract as #523, same
# statement-rowcount discipline, and the clear is payload-bound so a newer
# transition cannot be silently dropped by a slow in-flight delivery.
# ---------------------------------------------------------------------------


async def _pending_marker(db, combo_key):
    """Return `(row_exists, raw_json)` for the 30d pending-reversal marker.

    Existence is returned separately so a caller can assert the row is THERE
    before asserting on the column — a missing row would otherwise make an
    `is None` assertion pass vacuously.
    """
    cur = await db._conn.execute(
        "SELECT reversal_alert_pending_json FROM combo_performance "
        "WHERE combo_key = ? AND window = '30d'",
        (combo_key,),
    )
    row = await cur.fetchone()
    return row is not None, (row[0] if row else None)


async def _pending_payload(db, combo_key) -> dict:
    import json

    exists, raw = await _pending_marker(db, combo_key)
    assert exists, "30d row missing — payload assertions would be vacuous"
    assert raw is not None, "expected a pending reversal alert, found none"
    return json.loads(raw)


async def _count_pending_rows(db) -> int:
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM combo_performance "
        "WHERE reversal_alert_pending_json IS NOT NULL"
    )
    return (await cur.fetchone())[0]


async def test_failed_reversal_page_leaves_a_pending_marker(tmp_path, settings_factory):
    """(a) Telegram rejects -> the alert fact is DURABLE, not just logged."""
    import structlog
    from aioresponses import aioresponses

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_newly_suppressing_trades(db)

    with aioresponses() as m:
        m.post(_tg_url(s), status=500, body="rejected", repeat=True)
        with structlog.testing.capture_logs() as log_events:
            summary = await combo_refresh.refresh_all(db, s)  # must NOT raise

    row = await _get_combo_row(db, "gainers_early", "30d")
    assert row is not None and row["suppressed"] == 1

    payload = await _pending_payload(db, "gainers_early")
    assert payload["transition"] == "newly_suppressed"
    # The exact page is preserved, so the retry re-sends it verbatim rather
    # than re-deriving content from state that has since moved.
    assert "gainers_early" in payload["message"]
    assert "revive_signal_with_baseline" in payload["message"]
    datetime.fromisoformat(payload["detected_at"])

    assert summary["suppression_reversals"] == [], "counted as alerted on a 500"
    events = [e["event"] for e in log_events]
    assert events.count("suppression_reversal_alert_dispatched") == 1
    assert events.count("suppression_reversal_alert_failed") == 1
    assert "suppression_reversal_alert_delivered" not in events
    await db.close()


async def test_next_refresh_reattempts_pending_reversal_and_clears_it(
    tmp_path, settings_factory
):
    """(b) THE POINT OF THE CHANGE: the page survives the outage.

    Refresh 1 fails. Refresh 2 finds NO new transition (the combo is already
    suppressed) — the only reason it pages at all is the durable marker.
    """
    import structlog
    from aioresponses import aioresponses

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_newly_suppressing_trades(db)

    with aioresponses() as m:
        m.post(_tg_url(s), status=500, body="rejected", repeat=True)
        await combo_refresh.refresh_all(db, s)
    assert (await _pending_payload(db, "gainers_early"))["transition"] == (
        "newly_suppressed"
    )

    with aioresponses() as m:
        m.post(_tg_url(s), status=200, payload={"ok": True}, repeat=True)
        with structlog.testing.capture_logs() as log_events:
            summary = await combo_refresh.refresh_all(db, s)

    exists, raw = await _pending_marker(db, "gainers_early")
    assert exists, "30d row missing — the marker assertion would be vacuous"
    assert raw is None, "confirmed delivery did not clear the pending marker"

    assert summary["suppression_reversals"] == [
        {"combo_key": "gainers_early", "transition": "newly_suppressed"}
    ]
    events = [e["event"] for e in log_events]
    assert events.count("suppression_reversal_alert_dispatched") == 1, "double-paged"
    assert events.count("suppression_reversal_alert_delivered") == 1
    assert "suppression_reversal_alert_failed" not in events
    await db.close()


async def test_successful_first_attempt_leaves_no_pending_marker(
    tmp_path, settings_factory
):
    """(c) The happy path stamps and clears within the one refresh, so a clean
    run never leaves durable residue behind."""
    import structlog
    from aioresponses import aioresponses

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_newly_suppressing_trades(db)

    with aioresponses() as m:
        m.post(_tg_url(s), status=200, payload={"ok": True}, repeat=True)
        with structlog.testing.capture_logs() as log_events:
            summary = await combo_refresh.refresh_all(db, s)

    exists, raw = await _pending_marker(db, "gainers_early")
    assert exists and raw is None, "clean delivery left a pending marker behind"
    assert await _count_pending_rows(db) == 0

    assert summary["suppression_reversals"] == [
        {"combo_key": "gainers_early", "transition": "newly_suppressed"}
    ]
    events = [e["event"] for e in log_events]
    assert events.count("suppression_reversal_alert_dispatched") == 1
    assert events.count("suppression_reversal_alert_delivered") == 1
    assert "suppression_reversal_alert_failed" not in events
    await db.close()


async def test_repeated_failures_reattempt_every_refresh_without_duplicating(
    tmp_path, settings_factory
):
    """(d) Three failing refreshes -> three attempts, ONE pending row, and the
    original detection timestamp preserved.

    Also pins that the 30d UPSERT does not clobber the marker: `refresh_combo`
    rewrites this row on every one of these runs, and the pending payload has to
    survive all of them or the retry silently stops.
    """
    import structlog
    from aioresponses import aioresponses

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_newly_suppressing_trades(db)

    dispatched = failed = 0
    first_detected_at = None
    for _ in range(3):
        with aioresponses() as m:
            m.post(_tg_url(s), status=500, body="rejected", repeat=True)
            with structlog.testing.capture_logs() as log_events:
                await combo_refresh.refresh_all(db, s)
        events = [e["event"] for e in log_events]
        dispatched += events.count("suppression_reversal_alert_dispatched")
        failed += events.count("suppression_reversal_alert_failed")
        payload = await _pending_payload(db, "gainers_early")
        if first_detected_at is None:
            first_detected_at = payload["detected_at"]

    assert dispatched == 3, "a later refresh stopped re-attempting"
    assert failed == 3
    assert await _count_pending_rows(db) == 1, "duplicate pending state"
    payload = await _pending_payload(db, "gainers_early")
    assert payload["detected_at"] == first_detected_at, "retry rewrote the detection"
    assert payload["transition"] == "newly_suppressed"
    await db.close()


async def test_new_transition_supersedes_pending_and_logs_the_superseded_fact(
    tmp_path, settings_factory
):
    """Latest-wins: a NEW reversal while one is pending replaces the payload,
    and the superseded one is preserved in a structured log rather than dropped.

    Driven through `_process_suppression_reversals` with constructed snapshots:
    the classifier is covered separately, and building a real
    newly_suppressed-then-parole-exhausted sequence on top of an undelivered
    page would obscure what is under test. The alerter seam stays REAL.
    """
    import structlog
    from aioresponses import aioresponses

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_unsuppressed_combo_row(db, "flipper", trades=25, wins=5)

    unsuppressed = {
        "suppressed": 0,
        "parole_at": None,
        "win_rate_pct": 20.0,
        "trades": 25,
    }
    latched = {
        "suppressed": 1,
        "parole_at": "2026-08-01T00:00:00+00:00",
        "win_rate_pct": 20.0,
        "trades": 25,
    }
    reparoled = {
        "suppressed": 1,
        "parole_at": "2026-08-20T00:00:00+00:00",
        "win_rate_pct": 18.0,
        "trades": 30,
    }

    with aioresponses() as m:
        m.post(_tg_url(s), status=500, body="rejected", repeat=True)
        await combo_refresh._process_suppression_reversals(
            db, s, {"flipper": unsuppressed}, {"flipper": latched}
        )
    assert (await _pending_payload(db, "flipper"))["transition"] == "newly_suppressed"

    with aioresponses() as m:
        m.post(_tg_url(s), status=500, body="rejected", repeat=True)
        with structlog.testing.capture_logs() as log_events:
            await combo_refresh._process_suppression_reversals(
                db, s, {"flipper": latched}, {"flipper": reparoled}
            )

    payload = await _pending_payload(db, "flipper")
    assert (
        payload["transition"] == "parole_exhausted_resuppressed"
    ), "latest did not win"
    assert await _count_pending_rows(db) == 1

    superseded = [
        e for e in log_events if e["event"] == "suppression_reversal_alert_superseded"
    ]
    assert len(superseded) == 1, "the superseded page vanished without a trace"
    assert superseded[0]["superseded_transition"] == "newly_suppressed"
    assert superseded[0]["new_transition"] == "parole_exhausted_resuppressed"
    await db.close()


async def test_clear_is_payload_bound_so_a_midflight_supersede_survives(
    tmp_path, settings_factory, monkeypatch
):
    """The clear matches on the EXACT payload delivered.

    Delivery runs unlocked, so a concurrent refresh can supersede the pending
    page while it is in flight. A blind `SET ... = NULL` would drop that newer
    page — delivered one body, erased a different one. Mid-flight mutation is
    the thing under test here, so the sender is the seam that gets replaced
    (same idiom as the #523 generation-bound marker test); the real-alerter
    contract is covered by the tests above.
    """
    import json
    import structlog

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_unsuppressed_combo_row(db, "racer", trades=25, wins=5)
    old = json.dumps(
        {
            "transition": "newly_suppressed",
            "detected_at": "2026-08-13T00:00:00+00:00",
            "message": "old body",
        },
        sort_keys=True,
    )
    newer = json.dumps(
        {
            "transition": "parole_exhausted_resuppressed",
            "detected_at": "2026-08-13T01:00:00+00:00",
            "message": "newer body",
        },
        sort_keys=True,
    )
    await db._conn.execute(
        "UPDATE combo_performance SET reversal_alert_pending_json = ? "
        "WHERE combo_key = 'racer' AND window = '30d'",
        (old,),
    )
    await db._conn.commit()

    async def _supersede_midflight(settings, message):
        # Carried-over payload, so R1 stamps the detection time onto the body.
        assert message.startswith("old body"), "delivered the wrong body"
        assert "[detected 2026-08-13T00:00:00+00:00]" in message
        await db._conn.execute(
            "UPDATE combo_performance SET reversal_alert_pending_json = ? "
            "WHERE combo_key = 'racer' AND window = '30d'",
            (newer,),
        )
        await db._conn.commit()

    monkeypatch.setattr(
        combo_refresh, "_send_suppression_reversal_alert", _supersede_midflight
    )
    with structlog.testing.capture_logs() as log_events:
        await combo_refresh._process_suppression_reversals(db, s, {}, {})

    _, raw = await _pending_marker(db, "racer")
    assert raw == newer, "the newer pending page was clobbered by a blind clear"
    events = [e["event"] for e in log_events]
    # Neutral event name: rowcount 0 also occurs when the payload was already
    # CLEARED mid-flight, in which case nothing is pending — the old
    # "..._superseded_during_delivery" name asserted a state that is false half
    # the time (reviewer probe C).
    assert "suppression_reversal_pending_not_cleared" in events
    await db.close()


async def test_transition_without_a_30d_row_is_reported_not_swallowed(
    tmp_path, settings_factory
):
    """A page that cannot be made durable must be LOUD.

    `post_state` is built from 30d rows so this is defensive, but the failure it
    guards is a silently unrecorded alert — the exact class this PR exists to
    remove. Driven directly because refresh_all cannot produce the state.
    """
    import structlog

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    ghost = {"suppressed": 1, "parole_at": None, "win_rate_pct": 12.0, "trades": 30}

    with structlog.testing.capture_logs() as log_events:
        alerted = await combo_refresh._process_suppression_reversals(
            db, s, {}, {"ghost": ghost}
        )

    assert alerted == []
    missed = [
        e
        for e in log_events
        if e["event"] == "suppression_reversal_pending_write_missed"
    ]
    assert len(missed) == 1, "a page with nowhere to persist vanished silently"
    assert missed[0]["combo_key"] == "ghost"
    assert missed[0]["transition"] == "newly_suppressed"
    await db.close()


async def test_corrupt_pending_payload_is_reported_and_left_in_place(
    tmp_path, settings_factory
):
    """An undecodable payload is a real lost page.

    It is deliberately NOT cleared: a row that complains on every refresh is a
    far better failure mode than one that quietly deletes itself. Pins both
    halves — the error log AND the survival of the row.
    """
    import structlog

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_unsuppressed_combo_row(db, "corrupt", trades=25, wins=5)
    await db._conn.execute(
        "UPDATE combo_performance SET reversal_alert_pending_json = '{not json' "
        "WHERE combo_key = 'corrupt' AND window = '30d'"
    )
    await db._conn.commit()

    with structlog.testing.capture_logs() as log_events:
        alerted = await combo_refresh._process_suppression_reversals(db, s, {}, {})

    assert alerted == []
    events = [e["event"] for e in log_events]
    assert events.count("suppression_reversal_pending_unreadable") == 1
    assert "suppression_reversal_alert_dispatched" not in events
    _, raw = await _pending_marker(db, "corrupt")
    assert raw == "{not json", "corrupt payload was silently discarded"
    await db.close()


async def test_upgrade_adds_pending_reversal_column_to_existing_table(tmp_path):
    """(e) Upgrade-with-data: build the OLD shape, then migrate.

    A fresh `tmp_path` DB is created already-migrated and would never exercise
    the ALTER. Existing rows must land at NULL — "no pending alert", the correct
    pre-cutover state.
    """
    import aiosqlite

    path = tmp_path / "old.db"
    conn = await aiosqlite.connect(path)
    await conn.execute("""CREATE TABLE combo_performance (
               combo_key TEXT, window TEXT, trades INTEGER, wins INTEGER,
               losses INTEGER, total_pnl_usd REAL, avg_pnl_pct REAL,
               win_rate_pct REAL, suppressed INTEGER, suppressed_at TEXT,
               parole_at TEXT, parole_trades_remaining INTEGER,
               refresh_failures INTEGER, last_refreshed TEXT,
               perm_suppression_alerted_at TEXT,
               retest_incomplete_alerted_at TEXT,
               PRIMARY KEY (combo_key, window))""")
    await conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, avg_pnl_pct, "
        " win_rate_pct, suppressed, refresh_failures, last_refreshed) "
        "VALUES ('legacy', '30d', 40, 8, 32, -9.0, -1.0, 20.0, 1, 0, 'x')"
    )
    await conn.commit()
    await conn.close()

    db = Database(path)
    await db.initialize()
    cur = await db._conn.execute("PRAGMA table_info(combo_performance)")
    cols = {r[1] for r in await cur.fetchall()}
    assert "reversal_alert_pending_json" in cols, "ALTER did not land"

    cur = await db._conn.execute(
        "SELECT trades, suppressed, reversal_alert_pending_json "
        "FROM combo_performance WHERE combo_key = 'legacy'"
    )
    row = await cur.fetchone()
    assert row[0] == 40, "pre-existing data lost"
    assert row[1] == 1, "pre-existing suppression state lost"
    assert row[2] is None, "new column must default to NULL (no pending alert)"
    await db.close()


# ---------------------------------------------------------------------------
# LOOP-1 review fixes. The four tests below ORIGINATED AS PROBES written by the
# independent adversarial reviewer of fix/reversal-alert-durable-retry (probes
# A, D and E); they are promoted here as regression tests, adapted to house
# style and INVERTED where the probe asserted the defect rather than the fix.
# ---------------------------------------------------------------------------


async def test_retried_page_is_stamped_with_its_detection_time(
    tmp_path, settings_factory, monkeypatch
):
    """R1 (reviewer probe A): a page delivered on a LATER refresh must say when
    it was detected.

    A pending page outlives the state it describes — the suppression can be
    cleared by a later refresh, or by the operator doing exactly what the page
    told them to do. Delivered verbatim days later it asserts "the dispatcher
    now blocks its opens" about a combo that is trading again, and with no
    timestamp in the body it is byte-indistinguishable from a fresh page.

    First-attempt bodies stay byte-identical to #424, so the stamp appears only
    on a retry.
    """
    import scout.alerter as alerter_mod

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_newly_suppressing_trades(db)

    bodies: list[str] = []
    fail = {"on": True}

    async def fake_send(message, session, settings, **kw):
        bodies.append(message)
        if fail["on"]:
            raise RuntimeError("telegram 500")

    monkeypatch.setattr(alerter_mod, "send_telegram_message", fake_send)

    await combo_refresh.refresh_all(db, s)  # attempt 1 — rejected
    payload = await _pending_payload(db, "gainers_early")
    assert len(bodies) == 1
    assert "[detected " not in bodies[0], "first attempt must not carry a stamp"

    fail["on"] = False
    await combo_refresh.refresh_all(db, s)  # attempt 2 — delivered

    assert len(bodies) == 2
    retried = bodies[1]
    assert retried.endswith(f" [detected {payload['detected_at']}]"), retried
    assert retried.startswith(payload["message"]), "original body not preserved"
    await db.close()


async def test_first_attempt_delivery_body_carries_no_stamp(
    tmp_path, settings_factory, monkeypatch
):
    """The happy path must not gain a stamp — pins the other half of R1."""
    import scout.alerter as alerter_mod

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_newly_suppressing_trades(db)

    bodies: list[str] = []

    async def fake_send(message, session, settings, **kw):
        bodies.append(message)

    monkeypatch.setattr(alerter_mod, "send_telegram_message", fake_send)
    await combo_refresh.refresh_all(db, s)

    assert len(bodies) == 1
    assert "[detected " not in bodies[0]
    assert "auto-suppressed" in bodies[0]
    await db.close()


async def test_payload_without_a_detection_time_stamps_unknown_not_none(
    tmp_path, settings_factory, monkeypatch
):
    """A payload missing only `detected_at` must not render "[detected None]".

    Caught by the mutation battery: removing the guard left every other test
    green. `message` and `transition` are present so the page is deliverable,
    and a missing key is still classified as a retry — it just cannot say when.
    """
    import json

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_unsuppressed_combo_row(db, "nodate", trades=25, wins=5)
    await db._conn.execute(
        "UPDATE combo_performance SET reversal_alert_pending_json = ? "
        "WHERE combo_key = 'nodate' AND window = '30d'",
        (json.dumps({"transition": "newly_suppressed", "message": "body"}),),
    )
    await db._conn.commit()

    bodies: list[str] = []

    async def _capture(settings, message):
        bodies.append(message)

    monkeypatch.setattr(combo_refresh, "_send_suppression_reversal_alert", _capture)
    await combo_refresh._process_suppression_reversals(db, s, {}, {})

    assert bodies == ["body [detected unknown]"], bodies
    assert "None" not in bodies[0]
    await db.close()


async def test_db_error_in_record_phase_neither_aborts_refresh_nor_skips_siblings(
    tmp_path, settings_factory, monkeypatch
):
    """R2 (reviewer probe D): a DB error while recording a pending page must not
    take down the whole refresh.

    Before this fix the error propagated out of `refresh_all`, so the two
    sibling §12b passes — permanent-suppression and retest-terminal-incomplete —
    never ran. A new durability mechanism that can silence two OLDER alert paths
    is a net loss.
    """
    import aiosqlite
    import structlog

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_newly_suppressing_trades(db)

    real_execute = db._conn.execute
    perm_ran: list[int] = []
    retest_ran: list[int] = []

    async def _flaky(sql, *a, **kw):
        if "reversal_alert_pending_json = ?" in str(sql):
            raise aiosqlite.OperationalError("database is locked")
        return await real_execute(sql, *a, **kw)

    async def _perm(*a, **kw):
        perm_ran.append(1)
        return []

    async def _retest(*a, **kw):
        retest_ran.append(1)
        return []

    monkeypatch.setattr(db._conn, "execute", _flaky)
    monkeypatch.setattr(combo_refresh, "_process_permanent_suppression", _perm)
    monkeypatch.setattr(combo_refresh, "_process_retest_terminal_incomplete", _retest)

    with structlog.testing.capture_logs() as log_events:
        summary = await combo_refresh.refresh_all(db, s)  # must NOT raise

    monkeypatch.setattr(db._conn, "execute", real_execute)

    assert perm_ran == [1], "permanent-suppression pass was skipped"
    assert retest_ran == [1], "retest-incomplete pass was skipped"
    assert summary["suppression_reversals"] == []
    events = [e["event"] for e in log_events]
    assert events.count("suppression_reversal_pending_write_failed") == 1
    await db.close()


async def test_record_phase_failure_leaves_no_half_open_transaction(
    tmp_path, settings_factory
):
    """R2 (reviewer probe E): an `aiosqlite.Error` partway through the record
    loop must not strand an open transaction on the SHARED connection.

    The probe demonstrated `in_transaction` still True after the lock was
    released — the precise hazard this function's own comment warns about, and
    one that leaves the next writer inheriting a foreign transaction. Partial
    progress is kept: combos that DID record stay recorded.

    SCOPE: the guarantee covers `aiosqlite.Error` only, which is what the handler
    catches. A non-aiosqlite exception still propagates and can leave the
    transaction open — deliberately NOT widened, because no such exception is
    reachable from `refresh_all` on this path and a bare `except` here would
    swallow programming errors.
    """
    import aiosqlite

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    for combo in ("aaa", "zzz"):
        await _seed_unsuppressed_combo_row(db, combo, trades=25, wins=5)

    post = {
        c: {"suppressed": 1, "parole_at": None, "win_rate_pct": 10.0, "trades": 30}
        for c in ("aaa", "zzz")
    }

    real_execute = db._conn.execute
    seen = {"n": 0}

    async def _flaky(sql, *a, **kw):
        if "SET reversal_alert_pending_json = ?" in str(sql):
            seen["n"] += 1
            if seen["n"] == 2:  # the second combo blows up
                raise aiosqlite.OperationalError("database is locked")
        return await real_execute(sql, *a, **kw)

    db._conn.execute = _flaky
    await combo_refresh._record_pending_reversals(db, s, {}, post)  # must NOT raise
    db._conn.execute = real_execute

    assert (
        db._conn._conn.in_transaction is False
    ), "half-open transaction stranded on the shared connection"
    assert db._txn_lock.locked() is False

    _, first = await _pending_marker(db, "aaa")
    _, second = await _pending_marker(db, "zzz")
    assert first is not None, "successful combo lost its committed page"
    assert second is None, "the failed combo must not appear recorded"
    await db.close()


async def test_commit_failure_rolls_back_instead_of_leaving_a_half_open_txn(
    tmp_path, settings_factory
):
    """The OTHER half of R2: the per-combo guard protects the writes, but the
    final commit can fail on its own (disk I/O, lock timeout).

    Caught by my own mutation battery — removing the rollback left every other
    test green, i.e. this guard shipped unpinned. Without the rollback the
    shared connection stays inside a transaction the next writer inherits.

    F1: the rollback discards EVERY page this pass recorded, and those combos
    were never reported by the per-combo handler (it only fires for combos whose
    own UPDATE raised). So the commit-failure log must NAME them, or a commit
    failure destroys §12b pages silently. TWO combos, because with one the
    assertion cannot distinguish "names the lost pages" from "names something".
    """
    import aiosqlite
    import structlog

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    for combo in ("committer_one", "committer_two"):
        await _seed_unsuppressed_combo_row(db, combo, trades=25, wins=5)
    post = {
        c: {"suppressed": 1, "parole_at": None, "win_rate_pct": 10.0, "trades": 30}
        for c in ("committer_one", "committer_two")
    }

    real_commit = db._conn.commit

    async def _boom_commit():
        raise aiosqlite.OperationalError("disk I/O error")

    db._conn.commit = _boom_commit
    with structlog.testing.capture_logs() as log_events:
        await combo_refresh._record_pending_reversals(db, s, {}, post)  # no raise
    db._conn.commit = real_commit

    failed = [
        e
        for e in log_events
        if e["event"] == "suppression_reversal_pending_commit_failed"
    ]
    assert len(failed) == 1
    assert failed[0]["lost_combos"] == ["committer_one", "committer_two"], (
        "the discarded pages must be NAMED — they were never reported "
        "individually, so this log is their only trace"
    )
    assert failed[0]["lost_count"] == 2
    assert (
        db._conn._conn.in_transaction is False
    ), "rollback did not run — connection left half-open"
    for combo in ("committer_one", "committer_two"):
        _, raw = await _pending_marker(db, combo)
        assert raw is None, "a rolled-back write must not appear recorded"
    await db.close()


async def test_empty_string_marker_is_treated_as_present_but_corrupt(
    tmp_path, settings_factory
):
    """MINOR from review: `if existing:` skipped the supersede log for an
    empty-string marker, and a corrupt payload logged `superseded_message=None`,
    dropping the bytes that were the only evidence of what was lost.
    """
    import structlog

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_unsuppressed_combo_row(db, "emptymark", trades=25, wins=5)
    await db._conn.execute(
        "UPDATE combo_performance SET reversal_alert_pending_json = '' "
        "WHERE combo_key = 'emptymark' AND window = '30d'"
    )
    await db._conn.commit()

    post = {
        "emptymark": {
            "suppressed": 1,
            "parole_at": None,
            "win_rate_pct": 10.0,
            "trades": 30,
        }
    }
    with structlog.testing.capture_logs() as log_events:
        await combo_refresh._record_pending_reversals(db, s, {}, post)

    superseded = [
        e for e in log_events if e["event"] == "suppression_reversal_alert_superseded"
    ]
    assert len(superseded) == 1, "empty-string marker was treated as absent"
    assert superseded[0]["superseded_raw"] == "", "raw bytes must be preserved"
    assert superseded[0]["superseded_transition"] is None
    await db.close()


# ---------------------------------------------------------------------------
# FROZEN-CLOCK HAZARD — read this before adding freezegun (or any other clock
# freeze) to a test that touches suppression-reversal pages.
#
# The retry stamp is decided by `is_retry = detected_at != run_iso` in
# `_process_suppression_reversals`, where both sides come from
# `datetime.now(timezone.utc).isoformat()` in `scout/trading/combo_refresh.py`.
# That equality is the ONLY thing separating "recorded by this refresh" from
# "carried over from an earlier one".
#
# Freeze the clock and every refresh in the test gets the SAME `run_iso`, so a
# page carried across refreshes compares equal to the current run and is
# classified as a first attempt. The `[detected ...]` suffix silently vanishes
# — no error, no failed assertion anywhere except the one test that happens to
# check the suffix. A days-late page then reads as current, which is the exact
# operator-facing defect the stamp exists to prevent.
#
# So: these tests need a REAL, ADVANCING clock. If you need determinism, drive
# `detected_at` in the seeded payload (as
# `test_payload_without_a_detection_time_stamps_unknown_not_none` does) rather
# than freezing the source of `run_iso`.
#
# The two tests below pin both halves: the real-clock behavior that must hold,
# and a demonstration of what a freeze does to it.
# ---------------------------------------------------------------------------


class _ClockStub:
    """Stands in for the module's `datetime`, overriding only `now`.

    Everything else (`fromisoformat`, which combo_refresh also calls) falls
    through to the real class — a stub that shadowed the whole name would fail
    with AttributeError on an unrelated code path instead of testing the clock.
    """

    def __getattr__(self, name):
        return getattr(datetime, name)


class _RecordingClock(_ClockStub):
    """Real clock that records every `now()` the module asks for."""

    def __init__(self):
        self.seen: list[datetime] = []

    def now(self, tz=None):
        value = datetime.now(tz)
        self.seen.append(value)
        return value


class _FrozenClock(_ClockStub):
    """What freezegun does to the module: every `now()` returns one instant."""

    def __init__(self, fixed: datetime):
        self.fixed = fixed

    def now(self, tz=None):
        return self.fixed


async def test_carried_page_is_stamped_because_the_clock_advances(
    tmp_path, settings_factory, monkeypatch
):
    """Real-clock behavior, stated as the invariant it rests on.

    Two refreshes must observe two DISTINCT `now()` values; that distinctness
    is what makes a carried page classify as a retry and pick up its stamp.
    """
    import scout.alerter as alerter_mod

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_newly_suppressing_trades(db)

    clock = _RecordingClock()
    monkeypatch.setattr(combo_refresh, "datetime", clock)

    bodies: list[str] = []
    fail = {"on": True}

    async def fake_send(message, session, settings, **kw):
        bodies.append(message)
        if fail["on"]:
            raise RuntimeError("telegram 500")

    monkeypatch.setattr(alerter_mod, "send_telegram_message", fake_send)

    await combo_refresh.refresh_all(db, s)  # attempt 1 — rejected, stays pending
    payload = await _pending_payload(db, "gainers_early")
    fail["on"] = False
    await combo_refresh.refresh_all(db, s)  # attempt 2 — delivered as a retry

    assert len(bodies) == 2
    assert bodies[1].endswith(f" [detected {payload['detected_at']}]"), bodies[1]
    # The invariant underneath the assertion above. A clock that returned the
    # same instant twice would satisfy neither.
    stamps = {value.isoformat() for value in clock.seen}
    assert len(stamps) > 1, (
        "every now() returned the same instant — the retry stamp cannot work "
        "against a frozen clock"
    )
    await db.close()


async def test_frozen_clock_silently_un_stamps_a_carried_page(
    tmp_path, settings_factory, monkeypatch
):
    """HAZARD DEMONSTRATION — asserts the BROKEN behavior on purpose.

    This is not a defect being blessed; it is the failure mode being made
    visible so it is discovered here rather than in prod. Under a frozen
    clock the carried page's `detected_at` equals the current run's
    `run_iso`, so `is_retry` is False and the page goes out looking fresh.

    If a future change makes stamping robust to a frozen clock (e.g. by
    keying the retry off a delivery-attempt counter rather than timestamp
    equality), THIS TEST WILL FAIL — and the correct response is to delete it
    along with the warning block above, not to re-freeze anything.
    """
    import scout.alerter as alerter_mod

    _reset_tg_module_state()
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    await _seed_newly_suppressing_trades(db)

    frozen = _FrozenClock(datetime(2026, 8, 15, 12, 0, 0, tzinfo=timezone.utc))
    monkeypatch.setattr(combo_refresh, "datetime", frozen)

    bodies: list[str] = []
    fail = {"on": True}

    async def fake_send(message, session, settings, **kw):
        bodies.append(message)
        if fail["on"]:
            raise RuntimeError("telegram 500")

    monkeypatch.setattr(alerter_mod, "send_telegram_message", fake_send)

    await combo_refresh.refresh_all(db, s)
    fail["on"] = False
    await combo_refresh.refresh_all(db, s)

    assert len(bodies) == 2, "the page must still be carried and re-attempted"
    assert "[detected " not in bodies[1], (
        "if this now carries a stamp, timestamp equality is no longer what "
        "decides `is_retry` — delete this test and the warning block above"
    )
    await db.close()
