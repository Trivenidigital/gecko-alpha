"""End-to-end integration: suppression short-circuits signals dispatchers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
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


async def _seed_losers(db, coin_id):
    await db._conn.execute(
        "INSERT INTO losers_snapshots "
        "(coin_id, symbol, name, market_cap, "
        " price_change_24h, price_at_snapshot, snapshot_at) "
        "VALUES (?, 'S', 'N', 10000000, -50.0, 1.0, ?)",
        (coin_id, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


async def _seed_volume_spike(db, coin_id):
    """Volume spikes are passed as a list — no DB seed needed, caller passes list."""
    pass  # The spikes list is provided directly to the dispatcher.


@pytest.mark.parametrize(
    "combo_key,seed_fn,dispatcher_fn",
    [
        pytest.param(
            "gainers_early",
            _seed_gainers,
            lambda engine, db, s, coin_id: signals.trade_gainers(
                engine, db, min_mcap=1_000_000, settings=s
            ),
            id="gainers",
        ),
        pytest.param(
            "losers_contrarian",
            _seed_losers,
            lambda engine, db, s, coin_id: signals.trade_losers(
                engine, db, min_mcap=1_000_000, settings=s
            ),
            id="losers",
        ),
        pytest.param(
            "trending_catch",
            _seed_trending,
            lambda engine, db, s, coin_id: signals.trade_trending(
                engine, db, max_mcap_rank=1500, settings=s
            ),
            id="trending",
        ),
    ],
)
async def test_suppression_blocks_dispatcher(
    tmp_path, settings_factory, combo_key, seed_fn, dispatcher_fn
):
    """Suppressed combo_key must block trade opening for each dispatcher."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=s)
    coin_id = "test_coin"

    await _seed_price(db, coin_id, 1.0)
    await seed_fn(db, coin_id)
    await _seed_suppressed_combo(db, combo_key)

    await dispatcher_fn(engine, db, s, coin_id)

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE token_id = ?", (coin_id,)
    )
    assert (await cur.fetchone())[
        0
    ] == 0, f"Dispatcher for {combo_key!r} opened a trade despite suppression"
    await db.close()


async def test_suppression_blocks_volume_spikes(tmp_path, settings_factory):
    """trade_volume_spikes must respect suppression on 'volume_spike' combo."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=s)
    coin_id = "spike_coin"

    await _seed_price(db, coin_id, 2.0)
    await _seed_suppressed_combo(db, "volume_spike")

    spikes = [
        {
            "coin_id": coin_id,
            "symbol": "SC",
            "spike_ratio": 10.0,
            "current_price": 2.0,
        }
    ]
    await signals.trade_volume_spikes(engine, db, spikes, settings=s)

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE token_id = ?", (coin_id,)
    )
    assert (await cur.fetchone())[
        0
    ] == 0, "trade_volume_spikes opened a trade despite suppression on 'volume_spike'"
    await db.close()


async def test_suppression_blocks_first_signals(tmp_path, settings_factory):
    """trade_first_signals must respect suppression on the built combo_key.

    When signals_fired=['momentum_ratio'], the key is 'first_signal+momentum_ratio'.
    """
    from scout.models import CandidateToken
    from scout.trading.combo_key import build_combo_key

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=s)
    coin_id = "first_coin"

    signals_fired = ["momentum_ratio"]
    # Compute the actual key the dispatcher will use.
    combo_key = build_combo_key(signal_type="first_signal", signals=signals_fired)

    await _seed_price(db, coin_id, 1.5)
    await _seed_suppressed_combo(db, combo_key)

    token = CandidateToken(
        contract_address=coin_id,
        chain="coingecko",
        token_name="FirstCoin",
        ticker="FC",
        market_cap_usd=10_000_000,
    )
    scored = [(token, 30, signals_fired)]
    await signals.trade_first_signals(
        engine, db, scored, min_mcap=1_000_000, settings=s
    )

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE token_id = ?", (coin_id,)
    )
    assert (await cur.fetchone())[
        0
    ] == 0, f"trade_first_signals opened a trade despite suppression on {combo_key!r}"
    await db.close()


async def test_suppression_blocks_predictions(tmp_path, settings_factory):
    """trade_predictions must respect suppression on 'narrative_prediction' combo."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=s)
    coin_id = "pred_coin"

    await _seed_price(db, coin_id, 3.0)
    await _seed_suppressed_combo(db, "narrative_prediction")

    # Minimal prediction-like object
    pred = SimpleNamespace(
        coin_id=coin_id,
        is_control=False,
        market_cap_at_prediction=10_000_000,
        narrative_fit_score=5,
        category_name="defi",
    )
    await signals.trade_predictions(
        engine, db, [pred], min_mcap=1_000_000, min_fit_score=1, settings=s
    )

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE token_id = ?", (coin_id,)
    )
    assert (await cur.fetchone())[
        0
    ] == 0, (
        "trade_predictions opened a trade despite suppression on 'narrative_prediction'"
    )
    await db.close()


async def test_suppression_blocks_chain_completions(tmp_path, settings_factory):
    """trade_chain_completions must respect suppression on 'chain_completed' combo."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=s)
    coin_id = "chain_coin"

    await _seed_price(db, coin_id, 1.0)
    await _seed_suppressed_combo(db, "chain_completed")

    # Seed a chain_match row (within last 5 minutes) so the dispatcher picks it up.
    # We need chain_patterns to satisfy the FK if enforced — but FK is off in SQLite
    # by default. We'll seed chain_matches directly.
    await db._conn.execute(
        "INSERT INTO chain_matches "
        "(token_id, pipeline, pattern_id, pattern_name, steps_matched, "
        " total_steps, anchor_time, completed_at, chain_duration_hours, "
        " conviction_boost) "
        "VALUES (?, 'coingecko', 1, 'test_pattern', 3, 3, ?, ?, 1.0, 10)",
        (
            coin_id,
            datetime.now(timezone.utc).isoformat(),
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    await db._conn.commit()

    await signals.trade_chain_completions(engine, db, settings=s)

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE token_id = ?", (coin_id,)
    )
    assert (await cur.fetchone())[
        0
    ] == 0, "trade_chain_completions opened a trade despite suppression on 'chain_completed'"
    await db.close()


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
    """trade_first_signals must pass the full signals_fired list to
    build_combo_key so multi-signal combos get distinct keys.

    Drives the dispatcher with a real CandidateToken so the spy captures
    the actual call arguments, not just import-wiring.
    """
    from scout.trading import combo_key as ck_mod
    from scout.trading import signals as sig_mod
    from scout.models import CandidateToken
    from scout.trading.engine import TradingEngine

    db = Database(tmp_path / "t.db")
    await db.initialize()
    s = settings_factory(PAPER_STARTUP_WARMUP_SECONDS=0)
    engine = TradingEngine(mode="paper", db=db, settings=s)

    # Seed a price so the dispatcher can look up entry_price.
    await _seed_price(db, "spy_token", 1.23)

    captured: list[tuple] = []
    original = ck_mod.build_combo_key

    def _spy(*, signal_type, signals):
        captured.append((signal_type, tuple(signals) if signals else None))
        return original(signal_type=signal_type, signals=signals)

    monkeypatch.setattr(sig_mod, "build_combo_key", _spy)
    assert sig_mod.build_combo_key is _spy

    token = CandidateToken(
        contract_address="spy_token",
        chain="coingecko",
        token_name="SpyToken",
        ticker="SPY",
        market_cap_usd=10_000_000,
    )
    signals_fired = ["cg_trending_rank", "momentum_ratio"]
    scored_candidates = [(token, 30, signals_fired)]

    await sig_mod.trade_first_signals(
        engine, db, scored_candidates, min_mcap=1_000_000, settings=s
    )

    # The spy must have been called with signal_type="first_signal" and the
    # full signals_fired tuple so multi-signal combos get distinct keys.
    assert len(captured) == 1, f"build_combo_key not called exactly once: {captured}"
    sig_type, sigs_tuple = captured[0]
    assert sig_type == "first_signal"
    assert sigs_tuple == tuple(
        signals_fired
    ), f"signals passed to build_combo_key: {sigs_tuple}, expected: {tuple(signals_fired)}"
    await db.close()
