"""Tests for PaperTrader -- simulated trade execution with slippage."""

import asyncio
import json
from datetime import datetime, timezone

import pytest
from structlog.testing import capture_logs

from scout.db import Database
from scout.trading.paper import PaperTrader


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def trader():
    return PaperTrader()


async def test_execute_buy_inserts_trade(db, trader):
    """execute_buy creates a paper trade row in the DB."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        current_price=50000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=50,
        signal_combo="volume_spike",
        live_eligible_cap=20,
        min_quant_score=0,
    )
    assert trade_id is not None
    cursor = await db._conn.execute(
        "SELECT * FROM paper_trades WHERE id = ?", (trade_id,)
    )
    row = dict(await cursor.fetchone())
    assert row["token_id"] == "bitcoin"
    assert row["status"] == "open"


async def test_execute_buy_applies_slippage(db, trader):
    """Entry price includes slippage: effective_entry = price * (1 + bps/10000)."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        current_price=10000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=100,  # 1%
        signal_combo="volume_spike",
        live_eligible_cap=20,
        min_quant_score=0,
    )
    cursor = await db._conn.execute(
        "SELECT entry_price, quantity FROM paper_trades WHERE id = ?", (trade_id,)
    )
    row = dict(await cursor.fetchone())
    # effective_entry = 10000 * (1 + 100/10000) = 10100
    assert row["entry_price"] == pytest.approx(10100.0)
    # quantity = 1000 / 10100
    assert row["quantity"] == pytest.approx(1000.0 / 10100.0)


async def test_execute_buy_computes_tp_sl_prices(db, trader):
    """TP and SL prices are computed from effective entry price."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="ethereum",
        symbol="ETH",
        name="Ethereum",
        chain="coingecko",
        signal_type="narrative_prediction",
        signal_data={"fit": 85},
        current_price=3000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,  # no slippage
        signal_combo="narrative_prediction",
        live_eligible_cap=20,
        min_quant_score=0,
    )
    cursor = await db._conn.execute(
        "SELECT tp_price, sl_price FROM paper_trades WHERE id = ?", (trade_id,)
    )
    row = dict(await cursor.fetchone())
    # tp_price = 3000 * (1 + 20/100) = 3600
    assert row["tp_price"] == pytest.approx(3600.0)
    # sl_price = 3000 * (1 - 10/100) = 2700
    assert row["sl_price"] == pytest.approx(2700.0)


async def test_execute_sell_closes_trade(db, trader):
    """execute_sell closes a trade and computes PnL."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        current_price=50000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="volume_spike",
        live_eligible_cap=20,
        min_quant_score=0,
    )
    await trader.execute_sell(
        db=db,
        trade_id=trade_id,
        current_price=60000.0,
        reason="take_profit",
        slippage_bps=0,
    )
    cursor = await db._conn.execute(
        "SELECT status, exit_price, pnl_usd, pnl_pct FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = dict(await cursor.fetchone())
    assert row["status"] == "closed_tp"
    assert row["exit_price"] == pytest.approx(60000.0)
    assert row["pnl_pct"] == pytest.approx(20.0)
    assert row["pnl_usd"] == pytest.approx(200.0)


async def test_execute_sell_applies_exit_slippage(db, trader):
    """Exit price includes slippage: effective_exit = price * (1 - bps/10000)."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        current_price=10000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="volume_spike",
        live_eligible_cap=20,
        min_quant_score=0,
    )
    await trader.execute_sell(
        db=db,
        trade_id=trade_id,
        current_price=12000.0,
        reason="take_profit",
        slippage_bps=100,  # 1% exit slippage
    )
    cursor = await db._conn.execute(
        "SELECT exit_price FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = dict(await cursor.fetchone())
    # effective_exit = 12000 * (1 - 100/10000) = 11880
    assert row["exit_price"] == pytest.approx(11880.0)


async def test_execute_sell_stop_loss_pnl(db, trader):
    """PnL is negative on a stop loss."""
    trade_id = await trader.execute_buy(
        db=db,
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        current_price=50000.0,
        amount_usd=1000.0,
        tp_pct=20.0,
        sl_pct=10.0,
        slippage_bps=0,
        signal_combo="volume_spike",
        live_eligible_cap=20,
        min_quant_score=0,
    )
    await trader.execute_sell(
        db=db,
        trade_id=trade_id,
        current_price=45000.0,
        reason="stop_loss",
        slippage_bps=0,
    )
    cursor = await db._conn.execute(
        "SELECT pnl_usd, pnl_pct FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = dict(await cursor.fetchone())
    assert row["pnl_pct"] == pytest.approx(-10.0)
    assert row["pnl_usd"] == pytest.approx(-100.0)


# ---------------------------------------------------------------------------
# BL-060: would_be_live stamp tests
# ---------------------------------------------------------------------------


async def _stamp_of(db, trade_id: int) -> int | None:
    cur = await db._conn.execute(
        "SELECT would_be_live FROM paper_trades WHERE id=?", (trade_id,)
    )
    row = await cur.fetchone()
    return row[0] if row else None


@pytest.mark.asyncio
async def test_stamp_fresh_db_first_n_up_to_cap_are_live_eligible(tmp_path):
    """First cap inserts stamp =1; the (cap+1)th stamps =0."""
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader()

    results = []
    for i in range(21):  # cap=20; 21st should stamp =0
        trade_id = await trader.execute_buy(
            db=db,
            token_id=f"tok{i}",
            symbol=f"S{i}",
            name=f"Name{i}",
            chain="eth",
            signal_type="first_signal",
            signal_data={"quant_score": 50},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=40.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="first_signal",
            lead_time_vs_trending_min=None,
            lead_time_vs_trending_status=None,
            live_eligible_cap=20,
            min_quant_score=1,  # non-zero: stamps real 0/1
        )
        assert trade_id > 0, "trade_id must be populated"
        results.append(trade_id)

    async with db._conn.execute(
        "SELECT id, would_be_live FROM paper_trades ORDER BY id"
    ) as cur:
        rows = await cur.fetchall()
    stamps = [r[1] for r in rows]
    assert sum(s == 1 for s in stamps) == 20, f"first 20 must stamp =1; got {stamps}"
    assert stamps[20] == 0, f"21st must stamp =0; got {stamps[20]}"
    await db.close()


@pytest.mark.asyncio
async def test_closing_live_eligible_trade_frees_slot(tmp_path):
    """Closing a =1 trade frees its slot; next open gets =1."""
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader()

    async def open_one(i: int):
        return await trader.execute_buy(
            db=db,
            token_id=f"tok{i}",
            symbol=f"S{i}",
            name=f"N{i}",
            chain="eth",
            signal_type="first_signal",
            signal_data={"quant_score": 50},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=40.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="first_signal",
            lead_time_vs_trending_min=None,
            lead_time_vs_trending_status=None,
            live_eligible_cap=2,
            min_quant_score=1,
        )

    id1 = await open_one(0)
    id2 = await open_one(1)
    id3 = await open_one(2)
    assert (
        await _stamp_of(db, id1),
        await _stamp_of(db, id2),
        await _stamp_of(db, id3),
    ) == (1, 1, 0)

    await db._conn.execute(
        "UPDATE paper_trades SET status='closed_tp' WHERE id=?", (id1,)
    )
    await db._conn.commit()

    id4 = await open_one(99)
    assert await _stamp_of(db, id4) == 1, "slot freed by close; new open must be =1"
    await db.close()


@pytest.mark.asyncio
async def test_stamp_zero_fires_cap_reached_log(tmp_path):
    """Stamping =0 fires a paper_live_slot_cap_reached log with correct fields."""
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader()

    async def open_one(i: int):
        return await trader.execute_buy(
            db=db,
            token_id=f"tok{i}",
            symbol=f"S{i}",
            name=f"N{i}",
            chain="eth",
            signal_type="first_signal",
            signal_data={"quant_score": 50},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=40.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="first_signal",
            lead_time_vs_trending_min=None,
            lead_time_vs_trending_status=None,
            live_eligible_cap=1,
            min_quant_score=1,
        )

    await open_one(0)  # stamps =1, no log
    with capture_logs() as logs:
        await open_one(1)  # stamps =0, log fires
    events = [e for e in logs if e.get("event") == "paper_live_slot_cap_reached"]
    assert len(events) == 1, f"expected 1 cap log; got {events}"
    assert events[0]["cap"] == 1
    assert events[0]["signal_type"] == "first_signal"
    assert events[0]["signal_combo"] == "first_signal"
    assert events[0]["token_id"] == "tok1"
    await db.close()


@pytest.mark.asyncio
async def test_cap_zero_stamps_all_zero(tmp_path):
    """cap=0 means COUNT(*) < 0 is always False → ELSE branch → stamp=0."""
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader()

    for i in range(5):
        await trader.execute_buy(
            db=db,
            token_id=f"tok{i}",
            symbol=f"S{i}",
            name=f"N{i}",
            chain="eth",
            signal_type="first_signal",
            signal_data={"quant_score": 50},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=40.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="first_signal",
            lead_time_vs_trending_min=None,
            lead_time_vs_trending_status=None,
            live_eligible_cap=0,
            min_quant_score=1,
        )

    async with db._conn.execute("SELECT would_be_live FROM paper_trades") as cur:
        rows = await cur.fetchall()
    assert all(r[0] == 0 for r in rows), [r[0] for r in rows]
    await db.close()


@pytest.mark.asyncio
async def test_closed_live_eligible_excluded_from_cap_count(tmp_path):
    """Closed =1 trades do not count toward the cap (subquery filters status='open')."""
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader()

    # Seed 5 closed =1 rows directly
    for i in range(5):
        await db._conn.execute(
            "INSERT INTO paper_trades "
            "(token_id, symbol, name, chain, signal_type, signal_data, "
            "entry_price, amount_usd, quantity, tp_pct, sl_pct, "
            "tp_price, sl_price, status, opened_at, signal_combo, "
            "lead_time_vs_trending_min, lead_time_vs_trending_status, "
            "would_be_live) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                f"seeded{i}",
                "S",
                "N",
                "eth",
                "first_signal",
                "{}",
                1.0,
                100.0,
                100.0,
                40.0,
                20.0,
                1.4,
                0.8,
                "closed_tp",
                "2026-04-22T00:00:00",
                "first_signal",
                None,
                None,
                1,
            ),
        )
    await db._conn.commit()

    trade_id = await trader.execute_buy(
        db=db,
        token_id="fresh",
        symbol="F",
        name="Fresh",
        chain="eth",
        signal_type="first_signal",
        signal_data={"quant_score": 50},
        current_price=1.0,
        amount_usd=100.0,
        tp_pct=40.0,
        sl_pct=20.0,
        slippage_bps=0,
        signal_combo="first_signal",
        lead_time_vs_trending_min=None,
        lead_time_vs_trending_status=None,
        live_eligible_cap=2,
        min_quant_score=1,
    )
    cur = await db._conn.execute(
        "SELECT would_be_live FROM paper_trades WHERE id=?", (trade_id,)
    )
    row = await cur.fetchone()
    assert row[0] == 1, f"closed =1s should not block cap; got {row[0]}"
    await db.close()


@pytest.mark.asyncio
async def test_min_quant_score_zero_null_stamps(tmp_path):
    """min_quant_score=0 triggers the WHEN ? = 0 THEN NULL branch — all rows NULL."""
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader()

    for i in range(3):
        await trader.execute_buy(
            db=db,
            token_id=f"tok{i}",
            symbol=f"S{i}",
            name=f"N{i}",
            chain="eth",
            signal_type="first_signal",
            signal_data={"quant_score": 50},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=40.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="first_signal",
            lead_time_vs_trending_min=None,
            lead_time_vs_trending_status=None,
            live_eligible_cap=20,
            min_quant_score=0,
        )
    async with db._conn.execute("SELECT would_be_live FROM paper_trades") as cur:
        rows = await cur.fetchall()
    assert all(
        r[0] is None for r in rows
    ), f"regime-null stamps expected; got {[r[0] for r in rows]}"
    await db.close()


@pytest.mark.asyncio
async def test_stamped_rows_immutable_across_migrations(tmp_path):
    """Running _migrate_feedback_loop_schema twice must not alter open stamped rows."""
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader()

    trade_id = await trader.execute_buy(
        db=db,
        token_id="stable",
        symbol="S",
        name="N",
        chain="eth",
        signal_type="first_signal",
        signal_data={"quant_score": 50},
        current_price=1.0,
        amount_usd=100.0,
        tp_pct=40.0,
        sl_pct=20.0,
        slippage_bps=0,
        signal_combo="first_signal",
        lead_time_vs_trending_min=None,
        lead_time_vs_trending_status=None,
        live_eligible_cap=20,
        min_quant_score=1,
    )
    cur = await db._conn.execute(
        "SELECT would_be_live FROM paper_trades WHERE id=?", (trade_id,)
    )
    assert (await cur.fetchone())[0] == 1

    await db._migrate_feedback_loop_schema()
    await db._migrate_feedback_loop_schema()

    cur = await db._conn.execute(
        "SELECT status, would_be_live FROM paper_trades WHERE id=?", (trade_id,)
    )
    row = await cur.fetchone()
    assert row[0] == "open", f"status must stay open; got {row[0]}"
    assert row[1] == 1, f"stamped value must stay 1; got {row[1]}"
    await db.close()


@pytest.mark.asyncio
async def test_stamp_subquery_correctness_under_shared_conn(tmp_path):
    """40 asyncio.gather inserts on shared conn → exactly 20 =1 and 20 =0."""
    db = Database(str(tmp_path / "gecko.db"))
    await db.initialize()
    trader = PaperTrader()

    async def one(i: int):
        return await trader.execute_buy(
            db=db,
            token_id=f"tok{i}",
            symbol=f"S{i}",
            name=f"N{i}",
            chain="eth",
            signal_type="first_signal",
            signal_data={"quant_score": 50},
            current_price=1.0,
            amount_usd=100.0,
            tp_pct=40.0,
            sl_pct=20.0,
            slippage_bps=0,
            signal_combo="first_signal",
            lead_time_vs_trending_min=None,
            lead_time_vs_trending_status=None,
            live_eligible_cap=20,
            min_quant_score=1,
        )

    await asyncio.gather(*[one(i) for i in range(40)])
    async with db._conn.execute("SELECT would_be_live FROM paper_trades") as cur:
        stamps = [r[0] for r in await cur.fetchall()]
    ones = sum(1 for s in stamps if s == 1)
    zeros = sum(1 for s in stamps if s == 0)
    assert ones == 20 and zeros == 20, (
        f"expected 20/20 split; got ones={ones} zeros={zeros}. "
        "Note: aiosqlite serializes shared-conn ops at worker-thread level; "
        "this test proves subquery COUNT/CASE arithmetic is correct, NOT "
        "atomicity under true parallelism (see test_paper_trader_concurrency.py)"
    )
    await db.close()
