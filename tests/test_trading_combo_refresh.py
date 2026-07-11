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


async def test_chronic_failure_threshold_detected(tmp_path, settings_factory):
    """refresh_all returns combos whose 30d window refresh_failures >= threshold."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # Seed a combo with refresh_failures=3 (== default threshold) in 30d window.
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
    assert "permanent-suppression state" in stub.messages[0]
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


async def test_unsuppressed_zero_trade_combo_not_force_refreshed(
    tmp_path, settings_factory, monkeypatch
):
    """(vi) An UNSUPPRESSED combo with no recent trade is NOT force-refreshed —
    only suppressed combos get the widening."""
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
    # Untouched: sentinel trades=7 and last_refreshed both unchanged.
    row = await _get_combo_row(db, "quiet", "30d")
    assert row["trades"] == 7
    new_refreshed = await _scalar(
        db,
        "SELECT last_refreshed FROM combo_performance "
        "WHERE combo_key='quiet' AND window='30d'",
    )
    assert new_refreshed == old_refreshed
    assert "quiet" not in summary["permanent_suppression"]
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
    for _ in range(20):
        await _insert_trade(db, "gainers_early", -5, -3.0, now - timedelta(days=2))

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
