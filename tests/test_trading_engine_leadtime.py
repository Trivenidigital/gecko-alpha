"""Tests for lead-time computation and signal_combo persistence (spec §4.5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trading.engine import TradingEngine, _compute_lead_time_vs_trending


async def _seed_price(db, token_id: str, price: float):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (token_id, price, now),
    )
    await db._conn.commit()


async def _seed_trending(db, coin_id: str, snapshot_at: datetime):
    # trending_snapshots schema: id, coin_id, symbol, name, market_cap_rank,
    # trending_score, snapshot_at, created_at (see scout/db.py). No
    # `price_at_snapshot` column exists.
    await db._conn.execute(
        "INSERT INTO trending_snapshots "
        "(coin_id, symbol, name, market_cap_rank, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (coin_id, "SYM", "Name", 100, snapshot_at.isoformat()),
    )
    await db._conn.commit()


async def test_lead_time_negative_when_we_beat_trending(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Trending snapshot 15 minutes from now means we're opening 15min BEFORE trending.
    now = datetime.now(timezone.utc)
    crossed = now + timedelta(minutes=15)
    await _seed_trending(db, "coinX", crossed)
    lead, status = await _compute_lead_time_vs_trending(db, "coinX", now)
    assert status == "ok"
    assert lead is not None and lead < 0
    assert abs(lead - (-15)) < 0.5
    await db.close()


async def test_lead_time_positive_when_late(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    now = datetime.now(timezone.utc)
    crossed = now - timedelta(minutes=20)
    await _seed_trending(db, "coinX", crossed)
    lead, status = await _compute_lead_time_vs_trending(db, "coinX", now)
    assert status == "ok"
    assert lead is not None and lead > 0
    assert abs(lead - 20) < 0.5
    await db.close()


async def test_lead_time_no_reference_when_coin_never_trended(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    lead, status = await _compute_lead_time_vs_trending(
        db, "never_trended", datetime.now(timezone.utc)
    )
    assert lead is None
    assert status == "no_reference"
    await db.close()


async def test_lead_time_returns_error_status_on_bad_row(tmp_path, monkeypatch):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    # Insert a row with a malformed timestamp so datetime.fromisoformat raises.
    await db._conn.execute(
        "INSERT INTO trending_snapshots "
        "(coin_id, symbol, name, market_cap_rank, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("coinX", "SYM", "Name", 100, "NOT-A-TIMESTAMP"),
    )
    await db._conn.commit()
    lead, status = await _compute_lead_time_vs_trending(
        db, "coinX", datetime.now(timezone.utc)
    )
    assert lead is None
    assert status == "error"
    await db.close()


async def test_open_trade_persists_signal_combo_and_lead_time(
    tmp_path, settings_factory
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=settings)

    # Seed price so open_trade doesn't bail.
    await _seed_price(db, "coinX", 1.0)
    # Seed trending so lead_time computed as negative (we beat trending by 10 min).
    now = datetime.now(timezone.utc)
    await _seed_trending(db, "coinX", now + timedelta(minutes=10))

    tid = await engine.open_trade(
        token_id="coinX",
        symbol="CX",
        name="CoinX",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 3.0},
        entry_price=1.0,
        signal_combo="volume_spike",
    )
    assert tid is not None

    cur = await db._conn.execute(
        "SELECT signal_combo, lead_time_vs_trending_min, lead_time_vs_trending_status "
        "FROM paper_trades WHERE id = ?",
        (tid,),
    )
    row = await cur.fetchone()
    assert row["signal_combo"] == "volume_spike"
    assert row["lead_time_vs_trending_status"] == "ok"
    assert row["lead_time_vs_trending_min"] < 0  # beat trending
    await db.close()


async def test_open_trade_status_error_does_not_block_insert(
    tmp_path, settings_factory
):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=settings)

    await _seed_price(db, "coinX", 1.0)
    # Bad trending timestamp forces status='error'.
    await db._conn.execute(
        "INSERT INTO trending_snapshots "
        "(coin_id, symbol, name, market_cap_rank, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("coinX", "SYM", "Name", 100, "NOT-A-TIMESTAMP"),
    )
    await db._conn.commit()

    tid = await engine.open_trade(
        token_id="coinX",
        symbol="CX",
        name="CoinX",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        entry_price=1.0,
        signal_combo="volume_spike",
    )
    assert tid is not None  # trade still opens
    cur = await db._conn.execute(
        "SELECT lead_time_vs_trending_min, lead_time_vs_trending_status "
        "FROM paper_trades WHERE id = ?",
        (tid,),
    )
    row = await cur.fetchone()
    assert row["lead_time_vs_trending_min"] is None
    assert row["lead_time_vs_trending_status"] == "error"
    await db.close()


async def test_open_trade_without_signal_combo_raises(tmp_path, settings_factory):
    """signal_combo is a required kwarg — missing call site must fail loud."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    settings = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=settings)
    await _seed_price(db, "coinX", 1.0)
    with pytest.raises(TypeError):
        await engine.open_trade(
            token_id="coinX",
            symbol="CX",
            name="CoinX",
            chain="coingecko",
            signal_type="volume_spike",
            signal_data={},
            entry_price=1.0,
            # no signal_combo — must raise
        )
    await db.close()
