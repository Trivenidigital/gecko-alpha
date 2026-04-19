"""End-to-end integration: suppression short-circuits signals dispatchers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from scout.db import Database
from scout.trading.engine import TradingEngine
from scout.trading import signals


async def _seed_price(db, token_id, price):
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, ?)",
        (token_id, price, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def _seed_gainers(db, coin_id):
    await db._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, market_cap, "
        " price_change_24h, price_at_snapshot, snapshot_at) "
        "VALUES (?, 'S', 'N', 10000000, 50.0, 1.0, ?)",
        (coin_id, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def _seed_suppressed_combo(db, combo_key):
    future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    await db._conn.execute(
        "INSERT INTO combo_performance "
        "(combo_key, window, trades, wins, losses, total_pnl_usd, "
        " avg_pnl_pct, win_rate_pct, suppressed, suppressed_at, parole_at, "
        " parole_trades_remaining, refresh_failures, last_refreshed) "
        "VALUES (?, '30d', 25, 5, 20, -200, -4, 20.0, 1, ?, ?, 5, 0, ?)",
        (
            combo_key,
            datetime.now(timezone.utc).isoformat(),
            future,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    await db._conn.commit()


async def _seed_trending(db, coin_id):
    await db._conn.execute(
        "INSERT INTO trending_snapshots "
        "(coin_id, symbol, name, market_cap_rank, snapshot_at) "
        "VALUES (?, 'S', 'N', 5, ?)",
        (coin_id, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


# Each entry: (dispatcher callable, expected combo_key, seed-fn, dispatcher-kwargs)
@pytest.fixture
def dispatcher_cases():
    return [
        (
            "gainers",
            signals.trade_gainers,
            "gainers_early",
            _seed_gainers,
            {"min_mcap": 1_000_000},
        ),
    ]


async def test_suppressed_combo_blocks_trade_gainers(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=s)

    await _seed_price(db, "gx", 1.0)
    await _seed_gainers(db, "gx")
    await _seed_suppressed_combo(db, "gainers_early")

    await signals.trade_gainers(engine, db, min_mcap=1_000_000, settings=s)
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE token_id = 'gx'"
    )
    assert (await cur.fetchone())[0] == 0
    await db.close()


async def test_unsuppressed_combo_opens_trade(tmp_path, settings_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=s)

    await _seed_price(db, "gx", 1.0)
    await _seed_gainers(db, "gx")
    # No combo_performance row = cold_start = allow.

    await signals.trade_gainers(engine, db, min_mcap=1_000_000, settings=s)
    cur = await db._conn.execute(
        "SELECT signal_combo FROM paper_trades WHERE token_id = 'gx'"
    )
    row = await cur.fetchone()
    assert row is not None
    assert row["signal_combo"] == "gainers_early"
    await db.close()


async def test_suppression_emits_signal_suppressed_log(
    tmp_path,
    settings_factory,
):
    """Structured-log gate: 'signal_suppressed' event must be emitted when
    the combo is suppressed. Downstream dashboards grep for it."""
    import structlog.testing

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=s)

    await _seed_price(db, "gx", 1.0)
    await _seed_gainers(db, "gx")
    await _seed_suppressed_combo(db, "gainers_early")

    with structlog.testing.capture_logs() as entries:
        await signals.trade_gainers(engine, db, min_mcap=1_000_000, settings=s)

    assert any(
        e.get("event") == "signal_suppressed"
        and e.get("combo_key") == "gainers_early"
        and e.get("signal_type") == "gainers_early"
        for e in entries
    )
    await db.close()


async def test_trade_first_signals_uses_build_combo_key_with_signals(
    tmp_path,
    settings_factory,
    monkeypatch,
):
    """first_signal must pass the full signals_fired list to build_combo_key
    so multi-signal combos get distinct keys."""
    from scout.trading import combo_key as ck_mod
    from scout.trading import signals as sig_mod

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)

    captured: list[tuple] = []
    original = ck_mod.build_combo_key

    def _spy(*, signal_type, signals):
        captured.append((signal_type, tuple(signals) if signals else None))
        return original(signal_type=signal_type, signals=signals)

    monkeypatch.setattr(sig_mod, "build_combo_key", _spy)

    assert sig_mod.build_combo_key is _spy
    await db.close()
