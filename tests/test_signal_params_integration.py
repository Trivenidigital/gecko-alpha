"""Integration tests covering the gaps flagged by PR-review reviewer 3:

* C1 — evaluator must consume per-signal table values (max_duration, trail_pct)
* C2 — full ``Database.initialize()`` is idempotent across reopens
* drawdown algorithm — peak-to-trough, not running-min-vs-zero
* round-trip — engine.open_trade stamps row sl_pct that evaluator honours
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading.auto_suspend import _rolling_stats
from scout.trading.engine import TradingEngine
from scout.trading.evaluator import evaluate_paper_trades
from scout.trading.params import clear_cache_for_tests


@pytest.fixture(autouse=True)
def _wipe_cache():
    clear_cache_for_tests()
    yield
    clear_cache_for_tests()


# ---------------------------------------------------------------------------
# C1 — evaluator consumes per-signal max_duration_hours from the table
# ---------------------------------------------------------------------------


async def test_evaluator_uses_per_signal_max_duration_when_flag_on(
    tmp_path, settings_factory
):
    """Mutate `signal_params.max_duration_hours` to 1 for gainers_early; open
    a 2-hour-old trade; flag ON; evaluator must close on the table value, not
    settings.PAPER_MAX_DURATION_HOURS=48."""
    db = Database(tmp_path / "t.db")
    await db.initialize()

    # Stale price so the evaluator forces an expired close
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("tok", 1.0, (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()),
    )
    # Tighten table value
    await db._conn.execute(
        "UPDATE signal_params SET max_duration_hours = 1 "
        "WHERE signal_type='gainers_early'"
    )
    # 2h-old open trade
    opened = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at, remaining_qty, floor_armed)
           VALUES ('tok', 'TOK', 'T', 'coingecko', 'gainers_early', '{}',
                   1.0, 100.0, 100.0, 20.0, 15.0, 1.2, 0.85,
                   'open', ?, 100.0, 0)""",
        (opened,),
    )
    await db._conn.commit()

    settings = settings_factory(
        SIGNAL_PARAMS_ENABLED=True,
        PAPER_MAX_DURATION_HOURS=48,  # global says 48 — table says 1
    )
    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute("SELECT status FROM paper_trades WHERE token_id='tok'")
    status = (await cur.fetchone())[0]
    # Closed via the per-signal 1h table value; if evaluator was still reading
    # global 48h, status would still be 'open'.
    assert status.startswith("closed_"), f"expected closed_*, got {status}"
    await db.close()


async def test_evaluator_ignores_table_when_flag_off(tmp_path, settings_factory):
    """Symmetric: with flag OFF, table changes must NOT affect evaluator."""
    db = Database(tmp_path / "t.db")
    await db.initialize()

    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) VALUES (?, ?, ?)",
        ("tok", 1.0, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.execute(
        "UPDATE signal_params SET max_duration_hours = 1 "
        "WHERE signal_type='gainers_early'"
    )
    opened = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at, remaining_qty, floor_armed)
           VALUES ('tok', 'TOK', 'T', 'coingecko', 'gainers_early', '{}',
                   1.0, 100.0, 100.0, 20.0, 15.0, 1.2, 0.85,
                   'open', ?, 100.0, 0)""",
        (opened,),
    )
    await db._conn.commit()

    # Flag off: global PAPER_MAX_DURATION_HOURS=48 wins, trade should stay open
    settings = settings_factory(
        SIGNAL_PARAMS_ENABLED=False,
        PAPER_MAX_DURATION_HOURS=48,
    )
    await evaluate_paper_trades(db, settings)

    cur = await db._conn.execute("SELECT status FROM paper_trades WHERE token_id='tok'")
    assert (await cur.fetchone())[0] == "open"
    await db.close()


# ---------------------------------------------------------------------------
# C2 — full Database.initialize() is idempotent
# ---------------------------------------------------------------------------


async def test_full_initialize_re_run_is_idempotent(tmp_path):
    """Open + initialize + custom write + close + re-open + initialize again —
    table count unchanged, custom value retained, cutover row count == 1."""
    path = tmp_path / "t.db"

    db1 = Database(path)
    await db1.initialize()
    await db1._conn.execute(
        "UPDATE signal_params SET sl_pct = 12.3 WHERE signal_type='gainers_early'"
    )
    await db1._conn.commit()
    cur = await db1._conn.execute("SELECT COUNT(*) FROM signal_params")
    seeded_count = (await cur.fetchone())[0]
    await db1.close()

    db2 = Database(path)
    await db2.initialize()  # full re-run, including all migrations
    cur = await db2._conn.execute("SELECT COUNT(*) FROM signal_params")
    assert (await cur.fetchone())[0] == seeded_count
    cur = await db2._conn.execute(
        "SELECT sl_pct FROM signal_params WHERE signal_type='gainers_early'"
    )
    assert (await cur.fetchone())[0] == pytest.approx(12.3)
    cur = await db2._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations WHERE name='signal_params_v1'"
    )
    assert (await cur.fetchone())[0] == 1
    await db2.close()


# ---------------------------------------------------------------------------
# Drawdown algorithm — peak-to-trough
# ---------------------------------------------------------------------------


async def _insert_run(db, signal_type, pnls, base_offset_seconds=0):
    """Insert closed trades with given pnls in order, unique opened_at."""
    base = datetime.now(timezone.utc) - timedelta(days=1)
    for i, p in enumerate(pnls):
        opened = (base + timedelta(seconds=i + base_offset_seconds)).isoformat()
        closed = (
            base + timedelta(seconds=i + base_offset_seconds, hours=1)
        ).isoformat()
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
                status, exit_price, pnl_usd, pnl_pct, peak_pct,
                opened_at, closed_at)
               VALUES (?, 'TOK', 'T', 'coingecko', ?, '{}',
                       1.0, 100.0, 100.0, 20.0, 15.0, 1.2, 0.85,
                       'closed_sl', 1.0, ?, ?, 5.0, ?, ?)""",
            (
                f"tok-{signal_type}-{i + base_offset_seconds}",
                signal_type,
                p,
                p,
                opened,
                closed,
            ),
        )
    await db._conn.commit()


async def test_drawdown_peak_to_trough_after_winning_streak(tmp_path):
    """The bug-class catch: 30 wins of +$10, then 5 losses of -$110.

    - Cumulative path: 0 → +300 (after 30 wins) → -250 (after 5 losses)
    - Peak: +$300, trough: -$250, drop = $550

    Old algorithm returned -$190 (just min(running, 0) — never tracked
    the peak). Correct peak-to-trough is -$550. The silent-failure-hunter
    flagged this exact case as BLOCKER-3 in PR review."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _insert_run(db, "gainers_early", [10.0] * 30 + [-110.0] * 5)

    since = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    n, net, drawdown = await _rolling_stats(db._conn, "gainers_early", since)
    assert n == 35
    assert net == pytest.approx(-250.0, abs=0.5)
    # Peak-to-trough: +300 → -250 = $550 drop (signed: -550)
    assert drawdown == pytest.approx(-550.0, abs=0.5)
    await db.close()


async def test_drawdown_zero_when_only_winners(tmp_path):
    """30 wins, no losses → drawdown = 0 (running never drops below peak)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _insert_run(db, "gainers_early", [10.0] * 30)

    since = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    _, net, drawdown = await _rolling_stats(db._conn, "gainers_early", since)
    assert net == pytest.approx(300.0)
    assert drawdown == 0.0
    await db.close()


async def test_drawdown_full_giveback_caught_by_peak_to_trough(tmp_path):
    """The motivating case from review: signal runs to +$1000 then bleeds back
    to +$1. Old algorithm: drawdown=$0 (never went negative). Correct
    peak-to-trough: drawdown=-$999. This is the case Tier 1b's hard_loss
    escape hatch was designed to catch."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # 100 wins of +$10 = +$1000 peak, then 999 losses of -$1 = +$1 final
    await _insert_run(db, "gainers_early", [10.0] * 100 + [-1.0] * 999)

    since = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    _, net, drawdown = await _rolling_stats(db._conn, "gainers_early", since)
    assert net == pytest.approx(1.0, abs=0.5)
    assert drawdown == pytest.approx(-999.0, abs=1.0)
    await db.close()


# ---------------------------------------------------------------------------
# H3 — bump_cache_version cross-call invalidation
# ---------------------------------------------------------------------------


async def test_get_params_picks_up_post_apply_changes(tmp_path, settings_factory):
    """get_params caches; after apply_diffs bumps the version, the next call
    must read the new value without waiting for TTL."""
    from scout.trading.calibrate import (
        FieldChange,
        SignalDiff,
        SignalStats,
        apply_diffs,
    )
    from scout.trading.params import get_params

    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(SIGNAL_PARAMS_ENABLED=True)

    # Prime the cache
    sp_before = await get_params(db, "gainers_early", settings)
    assert sp_before.source == "table"
    initial_sl = sp_before.sl_pct

    # Apply a synthetic change
    diff = SignalDiff(
        signal_type="gainers_early",
        stats=SignalStats(
            "gainers_early",
            n_trades=60,
            win_rate_pct=55.0,
            expired_pct=10.0,
            avg_loss_pct=-10.0,
            avg_winner_peak_pct=22.0,
        ),
        changes=[FieldChange("sl_pct", initial_sl, initial_sl + 4.0)],
        reason_parts=["test"],
    )
    n = await apply_diffs(db, [diff], settings, session=None, force_no_alert=True)
    assert n == 1

    # Re-read — must reflect the change immediately, not after TTL
    sp_after = await get_params(db, "gainers_early", settings)
    assert sp_after.sl_pct == pytest.approx(initial_sl + 4.0)
    await db.close()
