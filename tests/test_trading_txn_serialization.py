"""Test that BEGIN...COMMIT blocks in suppression and combo_refresh are serialized.

The asyncio.Lock on Database._txn_lock prevents interleaving between
should_open (parole decrement) and refresh_combo (full refresh) within the
same event loop — even though aiosqlite dispatches to a single worker thread,
asyncio suspend points inside a BEGIN...COMMIT block create a race window.

This test runs them concurrently 20 times and asserts:
(a) No OperationalError("cannot start a transaction within a transaction")
(b) parole_trades_remaining ends at a deterministic value
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import combo_refresh, suppression


async def _seed_combo(db, combo_key, *, parole_remaining: int):
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    await db._conn.execute(
        "INSERT OR REPLACE INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, "
        " parole_at, parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES (?, '30d', 25, 5, 20, -100, -4, 20.0, 1, ?, ?, ?, 0, ?)",
        (combo_key, past, past, parole_remaining, past),
    )
    await db._conn.commit()


async def test_concurrent_refresh_and_should_open_no_txn_error(
    tmp_path, settings_factory
):
    """Run refresh_combo and should_open concurrently 20 times — must not raise
    'cannot start a transaction within a transaction' OperationalError."""
    db = Database(tmp_path / "txn_race.db")
    await db.initialize()
    s = settings_factory()
    combo_key = "race_test_combo"

    # Seed with plenty of parole remaining so should_open keeps decrementing.
    await _seed_combo(db, combo_key, parole_remaining=50)

    errors: list[Exception] = []

    async def _do_refresh():
        try:
            await combo_refresh.refresh_combo(db, combo_key, s)
        except Exception as e:
            errors.append(e)

    async def _do_should_open():
        try:
            await suppression.should_open(db, combo_key, settings=s)
        except Exception as e:
            errors.append(e)

    # 20 concurrent pairs of refresh + should_open
    tasks = []
    for _ in range(20):
        tasks.append(_do_refresh())
        tasks.append(_do_should_open())

    await asyncio.gather(*tasks)

    txn_errors = [
        e
        for e in errors
        if "cannot start a transaction within a transaction" in str(e).lower()
    ]
    assert not txn_errors, f"Transaction interleaving detected: {txn_errors[:3]}"
    await db.close()


async def test_concurrent_decrement_deterministic_final_value(
    tmp_path, settings_factory
):
    """With N concurrent should_open calls on parole_remaining=N, exactly N
    decrements must occur — final value must be 0 regardless of interleaving."""
    db = Database(tmp_path / "determ.db")
    await db.initialize()
    s = settings_factory()
    combo_key = "determ_combo"
    N = 5
    await _seed_combo(db, combo_key, parole_remaining=N)

    # N concurrent should_open calls — each tries to decrement once.
    results = await asyncio.gather(
        *[suppression.should_open(db, combo_key, settings=s) for _ in range(N)]
    )

    # All should succeed (parole_retest) since the lock serializes them.
    reasons = [r[1] for r in results]
    assert all(
        r in ("parole_retest", "parole_exhausted", "db_error_fallback_allow")
        for r in reasons
    ), f"Unexpected reasons: {reasons}"

    # Final remaining must be 0 (all N decremented from N).
    cur = await db._conn.execute(
        "SELECT parole_trades_remaining FROM combo_performance "
        "WHERE combo_key = ? AND window = '30d'",
        (combo_key,),
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == 0, (
        f"Expected parole_trades_remaining=0, got {row[0]}. " f"Results: {results}"
    )
    await db.close()
