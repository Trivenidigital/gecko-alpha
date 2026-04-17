"""Tests for TradingEngine -- pluggable interface with exposure and staleness checks."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.engine import TradingEngine


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        DB_PATH=tmp_path / "test.db",
        TRADING_ENABLED=True,
        TRADING_MODE="paper",
        PAPER_TRADE_AMOUNT_USD=1000.0,
        PAPER_MAX_EXPOSURE_USD=5000.0,
        PAPER_TP_PCT=20.0,
        PAPER_SL_PCT=10.0,
        PAPER_SLIPPAGE_BPS=50,
        PAPER_MAX_DURATION_HOURS=48,
        PAPER_MAX_OPEN_TRADES=1000,          # effectively off for most tests
        PAPER_STARTUP_WARMUP_SECONDS=0,      # off by default in tests
    )


@pytest.fixture
def engine(db, settings):
    return TradingEngine(mode="paper", db=db, settings=settings)


async def _seed_price_cache(db, coin_id, price, age_seconds=0):
    """Helper: insert a price_cache row with a given age."""
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d, market_cap, updated_at)
           VALUES (?, ?, 0, 0, 0, ?)""",
        (coin_id, price, ts.isoformat()),
    )
    await db._conn.commit()


async def test_open_trade_success(engine, db):
    """Engine opens a paper trade when price is available and fresh."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=60)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
    )
    assert trade_id is not None


async def test_open_trade_skips_no_price(engine, db):
    """Engine skips trade when price is not in cache."""
    trade_id = await engine.open_trade(
        token_id="unknown-coin",
        symbol="UNK",
        name="Unknown",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
    )
    assert trade_id is None


async def test_open_trade_skips_stale_price(engine, db):
    """Engine skips trade when price_cache.updated_at is older than _MAX_PRICE_AGE_SECONDS."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=7200)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
    )
    assert trade_id is None


async def test_open_trade_rejects_max_exposure(engine, db, settings):
    """Engine rejects trade when total exposure would exceed max."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    # Open 5 trades at $1000 each = $5000 (max)
    for i in range(5):
        ts = (datetime.now(timezone.utc) + timedelta(seconds=i)).isoformat()
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
                status, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (f"coin-{i}", "X", "X", "coingecko", "test", "{}",
             100.0, 1000.0, 10.0, 20.0, 10.0, 120.0, 90.0, ts),
        )
    await db._conn.commit()

    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
    )
    assert trade_id is None


async def test_open_trade_rejects_when_max_open_trades_hit(engine, db, settings):
    """Hard position-count cap: reject new trade when already at PAPER_MAX_OPEN_TRADES."""
    settings.PAPER_MAX_OPEN_TRADES = 3
    settings.PAPER_MAX_EXPOSURE_USD = 1_000_000  # take exposure cap out of the way

    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    for i in range(3):
        ts = (datetime.now(timezone.utc) + timedelta(seconds=i)).isoformat()
        await db._conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price, sl_price,
                status, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (f"coin-{i}", "X", "X", "coingecko", "test", "{}",
             100.0, 1000.0, 10.0, 20.0, 10.0, 120.0, 90.0, ts),
        )
    await db._conn.commit()

    trade_id = await engine.open_trade(
        token_id="bitcoin", symbol="BTC", chain="coingecko",
        signal_type="volume_spike", signal_data={},
    )
    assert trade_id is None


async def test_open_trade_warmup_blocks_initial_burst(db, settings):
    """During warmup, engine refuses to open trades (prevents restart-burst)."""
    import time

    settings.PAPER_STARTUP_WARMUP_SECONDS = 60
    settings.PAPER_MAX_OPEN_TRADES = 50

    engine = TradingEngine(mode="paper", db=db, settings=settings)
    # Engine records its start time on construction; freeze-less check:
    # with warmup=60s and start just now, a trade should be rejected.
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id = await engine.open_trade(
        token_id="bitcoin", symbol="BTC", chain="coingecko",
        signal_type="volume_spike", signal_data={},
    )
    assert trade_id is None


async def test_open_trade_after_warmup_proceeds(db, settings, monkeypatch):
    """After warmup elapses, trades open normally."""
    settings.PAPER_STARTUP_WARMUP_SECONDS = 1
    settings.PAPER_MAX_OPEN_TRADES = 50

    engine = TradingEngine(mode="paper", db=db, settings=settings)
    # Rewind engine start by more than warmup window
    engine._started_at = engine._started_at - 5

    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id = await engine.open_trade(
        token_id="bitcoin", symbol="BTC", chain="coingecko",
        signal_type="volume_spike", signal_data={},
    )
    assert trade_id is not None


async def test_open_trade_rejects_duplicate(engine, db):
    """Engine skips if same token already has an open trade."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id_1 = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
    )
    assert trade_id_1 is not None

    trade_id_2 = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
    )
    assert trade_id_2 is None


async def test_open_position_blocks_other_signal_types(engine, db):
    """Open position on token blocks ANY signal_type (exposure guard)."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id_1 = await engine.open_trade(
        token_id="bitcoin", symbol="BTC", chain="coingecko",
        signal_type="first_signal", signal_data={},
    )
    assert trade_id_1 is not None

    # Different signal_type — still blocked because first_signal is OPEN
    trade_id_2 = await engine.open_trade(
        token_id="bitcoin", symbol="BTC", chain="coingecko",
        signal_type="narrative_prediction", signal_data={},
    )
    assert trade_id_2 is None


async def test_closed_trade_allows_different_signal_type(engine, db):
    """Closed trade in last 48h does NOT block a different signal_type."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id_1 = await engine.open_trade(
        token_id="bitcoin", symbol="BTC", chain="coingecko",
        signal_type="first_signal", signal_data={},
    )
    assert trade_id_1 is not None
    # Force-close so no open position remains
    await engine.close_trade(trade_id_1, reason="test")

    # Different signal_type on same token — should succeed
    trade_id_2 = await engine.open_trade(
        token_id="bitcoin", symbol="BTC", chain="coingecko",
        signal_type="narrative_prediction", signal_data={},
    )
    assert trade_id_2 is not None


async def test_closed_trade_blocks_same_signal_type_within_48h(engine, db):
    """Same signal_type within 48h is still blocked (per-type cooldown)."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id_1 = await engine.open_trade(
        token_id="bitcoin", symbol="BTC", chain="coingecko",
        signal_type="first_signal", signal_data={},
    )
    assert trade_id_1 is not None
    await engine.close_trade(trade_id_1, reason="test")

    # Re-entry on same signal_type within cooldown — blocked
    trade_id_2 = await engine.open_trade(
        token_id="bitcoin", symbol="BTC", chain="coingecko",
        signal_type="first_signal", signal_data={},
    )
    assert trade_id_2 is None


async def test_close_trade(engine, db):
    """Engine can force-close a trade."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
    )
    await engine.close_trade(trade_id, reason="manual")
    cursor = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "closed_manual"


async def test_get_open_positions(engine, db):
    """get_open_positions returns all open trades."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    await _seed_price_cache(db, "ethereum", 3000.0, age_seconds=0)
    await engine.open_trade(
        token_id="bitcoin", symbol="BTC", name="Bitcoin",
        chain="coingecko", signal_type="volume_spike", signal_data={},
    )
    await engine.open_trade(
        token_id="ethereum", symbol="ETH", name="Ethereum",
        chain="coingecko", signal_type="narrative_prediction", signal_data={},
    )
    positions = await engine.get_open_positions()
    assert len(positions) == 2


async def test_open_trade_with_entry_price_skips_cache(engine, db):
    """Engine uses entry_price directly, bypassing price_cache lookup."""
    # No price_cache entry exists -- would normally be skipped
    trade_id = await engine.open_trade(
        token_id="trending-coin",
        symbol="TREND",
        name="TrendCoin",
        chain="coingecko",
        signal_type="trending_catch",
        signal_data={"source": "trending_snapshot"},
        entry_price=0.0042,
    )
    assert trade_id is not None
    cursor = await db._conn.execute(
        "SELECT entry_price FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == pytest.approx(0.0042, rel=0.01)


async def test_open_trade_entry_price_zero_falls_back_to_cache(engine, db):
    """entry_price=0 is treated as missing and falls back to price_cache."""
    # No cache entry -> should be skipped
    trade_id = await engine.open_trade(
        token_id="no-cache-coin",
        symbol="NC",
        name="NoCache",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={},
        entry_price=0.0,
    )
    assert trade_id is None


async def test_open_trade_entry_price_none_falls_back_to_cache(engine, db):
    """entry_price=None falls back to price_cache lookup (existing behaviour)."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        entry_price=None,
    )
    assert trade_id is not None


async def test_uses_custom_amount(engine, db):
    """Engine uses custom amount_usd if provided."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        amount_usd=2000.0,
    )
    cursor = await db._conn.execute(
        "SELECT amount_usd FROM paper_trades WHERE id = ?", (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == pytest.approx(2000.0)
