"""Tests for paper trade evaluator -- checkpoints, TP/SL, expiry, batch lookup."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading.evaluator import evaluate_paper_trades


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def _settings_factory(tmp_path, **overrides):
    from scout.config import Settings
    defaults = dict(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        DB_PATH=tmp_path / "test.db",
        PAPER_TP_PCT=20.0,
        PAPER_SL_PCT=10.0,
        PAPER_SLIPPAGE_BPS=0,
        PAPER_MAX_DURATION_HOURS=48,
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def _insert_trade(db, token_id, entry_price, opened_at, **kwargs):
    """Helper: insert a paper trade for testing."""
    defaults = {
        "symbol": token_id.upper()[:3],
        "name": token_id.title(),
        "chain": "coingecko",
        "signal_type": "volume_spike",
        "signal_data": json.dumps({}),
        "amount_usd": 1000.0,
        "quantity": 1000.0 / entry_price,
        "tp_pct": 20.0,
        "sl_pct": 10.0,
        "tp_price": entry_price * 1.2,
        "sl_price": entry_price * 0.9,
        "status": "open",
    }
    defaults.update(kwargs)
    cursor = await db._conn.execute(
        """INSERT INTO paper_trades
           (token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
            status, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            token_id, defaults["symbol"], defaults["name"], defaults["chain"],
            defaults["signal_type"], defaults["signal_data"],
            entry_price, defaults["amount_usd"], defaults["quantity"],
            defaults["tp_pct"], defaults["sl_pct"],
            defaults["tp_price"], defaults["sl_price"],
            defaults["status"], opened_at.isoformat(),
        ),
    )
    await db._conn.commit()
    return cursor.lastrowid


async def _seed_price(db, coin_id, price):
    """Helper: insert fresh price into cache."""
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d, market_cap, updated_at)
           VALUES (?, ?, 0, 0, 0, ?)""",
        (coin_id, price, now),
    )
    await db._conn.commit()


async def test_checkpoint_1h_update(db, tmp_path):
    """Evaluator updates 1h checkpoint when 1h has elapsed."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(hours=1, minutes=5)
    trade_id = await _insert_trade(db, "bitcoin", 50000.0, opened)
    await _seed_price(db, "bitcoin", 55000.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT checkpoint_1h_price, checkpoint_1h_pct FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == pytest.approx(55000.0)
    assert row[1] == pytest.approx(10.0)


async def test_tp_closure(db, tmp_path):
    """Evaluator closes trade when price >= tp_price."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(db, "bitcoin", 50000.0, opened)
    # TP at 60000, current at 61000
    await _seed_price(db, "bitcoin", 61000.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "closed_tp"
    assert row[1] == "take_profit"


async def test_sl_closure(db, tmp_path):
    """Evaluator closes trade when price <= sl_price."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(db, "bitcoin", 50000.0, opened)
    # SL at 45000, current at 44000
    await _seed_price(db, "bitcoin", 44000.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "closed_sl"
    assert row[1] == "stop_loss"


async def test_expiry_closure(db, tmp_path):
    """Evaluator closes trade after PAPER_MAX_DURATION_HOURS."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(hours=49)
    trade_id = await _insert_trade(db, "bitcoin", 50000.0, opened)
    await _seed_price(db, "bitcoin", 51000.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT status, exit_reason FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "closed_expired"
    assert row[1] == "expired"


async def test_peak_tracking(db, tmp_path):
    """Evaluator updates peak_price when current > previous peak."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(db, "bitcoin", 50000.0, opened)
    # Price is up but not at TP
    await _seed_price(db, "bitcoin", 55000.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT peak_price, peak_pct FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == pytest.approx(55000.0)
    assert row[1] == pytest.approx(10.0)


async def test_batch_price_lookup(db, tmp_path):
    """Evaluator uses a single batch query for all open trades."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    await _insert_trade(db, "bitcoin", 50000.0, opened)
    await _insert_trade(db, "ethereum", 3000.0, opened)
    await _seed_price(db, "bitcoin", 52000.0)
    await _seed_price(db, "ethereum", 3100.0)

    # This should not raise -- batch query handles multiple coins
    await evaluate_paper_trades(db, settings)

    # Both trades should have peak tracking updated
    cursor = await db._conn.execute(
        "SELECT token_id, peak_price FROM paper_trades WHERE peak_price IS NOT NULL"
    )
    rows = await cursor.fetchall()
    assert len(rows) == 2


async def test_tp_with_checkpoint(db, tmp_path):
    """TP/SL takes priority but checkpoint is also recorded."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(hours=1, minutes=5)
    trade_id = await _insert_trade(db, "bitcoin", 50000.0, opened)
    # Price at TP level -- should close AND record 1h checkpoint
    await _seed_price(db, "bitcoin", 62000.0)

    await evaluate_paper_trades(db, settings)

    cursor = await db._conn.execute(
        "SELECT status, checkpoint_1h_price FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "closed_tp"
    assert row[1] is not None  # 1h checkpoint was recorded


async def test_skips_trade_with_no_price(db, tmp_path):
    """Evaluator skips trades where price is not available in cache."""
    settings = _settings_factory(tmp_path)
    opened = datetime.now(timezone.utc) - timedelta(minutes=30)
    trade_id = await _insert_trade(db, "unknown-coin", 100.0, opened)
    # No price in cache for unknown-coin

    await evaluate_paper_trades(db, settings)

    # Trade should remain open, unchanged
    cursor = await db._conn.execute(
        "SELECT status, peak_price FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "open"
    assert row[1] is None
