"""Tests for the late-pump filter on gainers and the filter-rejection logging
for trade_volume_spikes / trade_predictions that lacked log parity."""

from datetime import datetime, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.engine import TradingEngine
from scout.trading.signals import (
    trade_gainers,
    trade_predictions,
    trade_volume_spikes,
)


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
        PAPER_TRADE_AMOUNT_USD=300.0,
        PAPER_MAX_EXPOSURE_USD=200_000.0,
        PAPER_MIN_MCAP=5_000_000,
        PAPER_MAX_MCAP_RANK=1500,
        PAPER_MAX_OPEN_TRADES=77,
        PAPER_STARTUP_WARMUP_SECONDS=0,
        PAPER_GAINERS_MAX_24H_PCT=50.0,
    )


@pytest.fixture
def engine(db, settings):
    return TradingEngine(mode="paper", db=db, settings=settings)


async def _insert_gainer_with_change(
    db, coin_id, price_change_24h, market_cap=10_000_000
):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            coin_id,
            coin_id.upper(),
            coin_id,
            price_change_24h,
            market_cap,
            100_000.0,
            1.0,
            now,
        ),
    )
    await db._conn.commit()


async def _open_count(db):
    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status = 'open'"
    )
    row = await cursor.fetchone()
    return row[0]


async def test_trade_gainers_skips_late_pump(db, engine, settings):
    """Token with 24h change > PAPER_GAINERS_MAX_24H_PCT must not open a trade."""
    await _insert_gainer_with_change(db, "late-pump", price_change_24h=80.0)
    await trade_gainers(engine, db, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_gainers_opens_below_threshold(db, engine, settings):
    """Token with 24h change within threshold should still trade."""
    await _insert_gainer_with_change(db, "fresh-pump", price_change_24h=25.0)
    await trade_gainers(engine, db, settings=settings)
    assert await _open_count(db) == 1


async def test_trade_gainers_filter_log_counts_late_pumps(db, engine, settings, capsys):
    """The filter summary log must include skipped_late_pump count."""
    await _insert_gainer_with_change(db, "late-1", price_change_24h=60.0)
    await _insert_gainer_with_change(db, "late-2", price_change_24h=75.0)
    await _insert_gainer_with_change(db, "fresh", price_change_24h=20.0)
    await trade_gainers(engine, db, settings=settings)

    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "trade_gainers_filtered" in combined
    assert "skipped_late_pump" in combined
    assert await _open_count(db) == 1  # only 'fresh' opened


async def test_trade_volume_spikes_emits_filter_log(db, engine, settings, capsys):
    """Volume-spike trigger must log attempt/filter counts (parity with trade_gainers)."""
    await trade_volume_spikes(engine, db, spikes=[], settings=settings)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "trade_volume_spikes_filtered" in combined


async def test_trade_predictions_emits_filter_log(db, engine, settings, capsys):
    """Narrative prediction trigger must log attempt/filter counts."""
    await trade_predictions(engine, db, prediction_models=[], settings=settings)
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "trade_predictions_filtered" in combined
