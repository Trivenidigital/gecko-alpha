"""BL-063 moonshot arm tests — atomicity, idempotency, race safety."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.exceptions import MoonshotArmFailed
from scout.trading.paper import PaperTrader


async def _open_trade(db: Database, trader: PaperTrader, *, token_id: str) -> int:
    """Open a paper trade at $1.00 with the BL-061 ladder defaults."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id=token_id,
        symbol=token_id.upper(),
        name=token_id.title(),
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        current_price=1.00,
        amount_usd=100.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="first_signal",
    )
    assert trade_id is not None
    return trade_id


@pytest.mark.asyncio
async def test_arm_moonshot_writes_fields(tmp_path):
    """First call returns the timestamp it wrote and stores the fields."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    trade_id = await _open_trade(db, trader, token_id="t1")

    returned = await trader.arm_moonshot(
        db, trade_id, peak_pct_at_arm=42.0, original_trail_drawdown_pct=12.0
    )

    # Returns the ISO timestamp written to the row — callers use it directly
    # rather than re-stamping a microsecond-drifted datetime.now().
    assert isinstance(returned, str)
    parsed = datetime.fromisoformat(returned)
    assert parsed.tzinfo is not None
    cur = await db._conn.execute(
        "SELECT moonshot_armed_at, original_trail_drawdown_pct "
        "FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    armed_at, original_trail = await cur.fetchone()
    assert armed_at == returned  # exact match — no drift
    assert original_trail == pytest.approx(12.0)
    await db.close()


@pytest.mark.asyncio
async def test_arm_moonshot_idempotent(tmp_path):
    """Second call returns None without overwriting fields."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    trade_id = await _open_trade(db, trader, token_id="t2")

    first = await trader.arm_moonshot(
        db, trade_id, peak_pct_at_arm=42.0, original_trail_drawdown_pct=12.0
    )
    cur = await db._conn.execute(
        "SELECT moonshot_armed_at FROM paper_trades WHERE id = ?", (trade_id,)
    )
    (first_armed_at,) = await cur.fetchone()

    second = await trader.arm_moonshot(
        db, trade_id, peak_pct_at_arm=99.0, original_trail_drawdown_pct=999.0
    )

    cur = await db._conn.execute(
        "SELECT moonshot_armed_at, original_trail_drawdown_pct "
        "FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    second_armed_at, second_trail = await cur.fetchone()

    assert isinstance(first, str)
    assert second is None
    # Original timestamp + trail preserved — not overwritten by the second call.
    assert second_armed_at == first_armed_at
    assert second_trail == pytest.approx(12.0)
    await db.close()


@pytest.mark.asyncio
async def test_arm_moonshot_serialized_idempotency(tmp_path):
    """Two concurrent arm_moonshot calls — exactly one returns a timestamp,
    the other returns None.

    Note: aiosqlite serialises queries through a single writer thread, so
    this test verifies SQL-level WHERE-clause atomicity rather than true
    OS-thread concurrency. That's still the only guarantee we depend on.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()
    trade_id = await _open_trade(db, trader, token_id="t3")

    results = await asyncio.gather(
        trader.arm_moonshot(
            db, trade_id, peak_pct_at_arm=42.0, original_trail_drawdown_pct=12.0
        ),
        trader.arm_moonshot(
            db, trade_id, peak_pct_at_arm=43.0, original_trail_drawdown_pct=12.0
        ),
    )

    timestamps = [r for r in results if isinstance(r, str)]
    nones = [r for r in results if r is None]
    assert len(timestamps) == 1
    assert len(nones) == 1
    await db.close()


@pytest.mark.asyncio
async def test_arm_moonshot_missing_trade_raises(tmp_path):
    """Arming a non-existent trade raises MoonshotArmFailed (not silent False)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    trader = PaperTrader()

    with pytest.raises(MoonshotArmFailed, match="trade_id=99999"):
        await trader.arm_moonshot(
            db, 99999, peak_pct_at_arm=42.0, original_trail_drawdown_pct=12.0
        )
    await db.close()
