"""Perf gate for refresh_all — spec §12.

Runs by default (no marker). Keep the assertion tolerant (5s) so CI
variance doesn't cause false failures; tighten once we have historical
baselines from the VPS.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading import combo_refresh


async def test_refresh_all_under_5s_for_1000_trades(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory()
    now = datetime.now(timezone.utc)
    # 50 combos × 20 trades each.
    for i in range(50):
        combo = f"combo_{i:02d}"
        for j in range(20):
            await db._conn.execute(
                "INSERT INTO paper_trades "
                "(token_id, symbol, name, chain, signal_type, signal_data, "
                " entry_price, amount_usd, quantity, tp_pct, sl_pct, "
                " tp_price, sl_price, status, pnl_usd, pnl_pct, "
                " opened_at, closed_at, signal_combo) "
                "VALUES (?, 'S', 'N', 'cg', 'volume_spike', '{}', "
                " 1, 100, 100, 20, 10, 1.2, 0.9, 'closed_tp', 10, 5, ?, ?, ?)",
                (
                    f"tok_{i}_{j}",
                    (now - timedelta(days=5)).isoformat(),
                    (now - timedelta(days=4)).isoformat(),
                    combo,
                ),
            )
    await db._conn.commit()
    t0 = time.monotonic()
    result = await combo_refresh.refresh_all(db, s)
    elapsed = time.monotonic() - t0
    assert result["refreshed"] == 50
    assert elapsed < 5.0, f"refresh_all took {elapsed:.2f}s (>5s gate)"
    await db.close()
