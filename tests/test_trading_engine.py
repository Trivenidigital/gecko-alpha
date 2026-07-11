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
        PAPER_MAX_OPEN_TRADES=1000,  # effectively off for most tests
        PAPER_STARTUP_WARMUP_SECONDS=0,  # off by default in tests
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
        signal_combo="volume_spike",
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
        signal_combo="volume_spike",
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
        signal_combo="volume_spike",
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
            (
                f"coin-{i}",
                "X",
                "X",
                "coingecko",
                "test",
                "{}",
                100.0,
                1000.0,
                10.0,
                20.0,
                10.0,
                120.0,
                90.0,
                ts,
            ),
        )
    await db._conn.commit()

    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        signal_combo="volume_spike",
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
            (
                f"coin-{i}",
                "X",
                "X",
                "coingecko",
                "test",
                "{}",
                100.0,
                1000.0,
                10.0,
                20.0,
                10.0,
                120.0,
                90.0,
                ts,
            ),
        )
    await db._conn.commit()

    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        signal_combo="volume_spike",
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
        token_id="bitcoin",
        symbol="BTC",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        signal_combo="volume_spike",
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
        token_id="bitcoin",
        symbol="BTC",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        signal_combo="volume_spike",
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
        signal_combo="volume_spike",
    )
    assert trade_id_1 is not None

    trade_id_2 = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        signal_combo="volume_spike",
    )
    assert trade_id_2 is None


async def test_open_position_blocks_other_signal_types(engine, db):
    """Open position on token blocks ANY signal_type (exposure guard)."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id_1 = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        signal_combo="first_signal",
    )
    assert trade_id_1 is not None

    # Different signal_type — still blocked because first_signal is OPEN
    trade_id_2 = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        chain="coingecko",
        signal_type="chain_completed",
        signal_data={},
        signal_combo="chain_completed",
    )
    assert trade_id_2 is None


async def test_closed_trade_allows_different_signal_type(engine, db):
    """Closed trade in last 48h does NOT block a different signal_type."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id_1 = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        signal_combo="first_signal",
    )
    assert trade_id_1 is not None
    # Force-close so no open position remains
    await engine.close_trade(trade_id_1, reason="test")

    # Different signal_type on same token — should succeed
    trade_id_2 = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        chain="coingecko",
        signal_type="chain_completed",
        signal_data={},
        signal_combo="chain_completed",
    )
    assert trade_id_2 is not None


async def test_closed_trade_blocks_same_signal_type_within_48h(engine, db):
    """Same signal_type within 48h is still blocked (per-type cooldown)."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    trade_id_1 = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        signal_combo="first_signal",
    )
    assert trade_id_1 is not None
    await engine.close_trade(trade_id_1, reason="test")

    # Re-entry on same signal_type within cooldown — blocked
    trade_id_2 = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        chain="coingecko",
        signal_type="first_signal",
        signal_data={},
        signal_combo="first_signal",
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
        signal_combo="volume_spike",
    )
    await engine.close_trade(trade_id, reason="manual")
    cursor = await db._conn.execute(
        "SELECT status FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == "closed_manual"


async def test_get_open_positions(engine, db):
    """get_open_positions returns all open trades."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=0)
    await _seed_price_cache(db, "ethereum", 3000.0, age_seconds=0)
    await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={},
        signal_combo="volume_spike",
    )
    await engine.open_trade(
        token_id="ethereum",
        symbol="ETH",
        name="Ethereum",
        chain="coingecko",
        signal_type="chain_completed",
        signal_data={},
        signal_combo="chain_completed",
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
        signal_combo="trending_catch",
    )
    assert trade_id is not None
    cursor = await db._conn.execute(
        "SELECT entry_price FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == pytest.approx(0.0042, rel=0.01)


async def test_open_trade_stamps_non_actionable_but_still_opens(engine, db):
    trade_id = await engine.open_trade(
        token_id="loser-probe",
        symbol="LP",
        name="LoserProbe",
        chain="coingecko",
        signal_type="losers_contrarian",
        signal_data={"mcap": 20_000_000},
        entry_price=1.0,
        signal_combo="losers_contrarian",
    )
    assert trade_id is not None
    cursor = await db._conn.execute(
        "SELECT actionable, actionability_reason, actionability_version "
        "FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row["actionable"] == 0
    assert row["actionability_reason"] == "v1_block_losers_contrarian_exploratory"
    assert row["actionability_version"] == "v1"


async def test_open_trade_enriches_actionability_mcap_from_price_cache(engine, db):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT OR REPLACE INTO price_cache "
        "(coin_id, current_price, market_cap, updated_at) VALUES (?, ?, ?, ?)",
        ("vol-no-mcap", 1.0, 20_000_000, now),
    )
    await db._conn.commit()

    trade_id = await engine.open_trade(
        token_id="vol-no-mcap",
        symbol="VNM",
        name="VolumeNoMcap",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        entry_price=1.0,
        signal_combo="volume_spike",
    )
    assert trade_id is not None
    cursor = await db._conn.execute(
        "SELECT actionable, actionability_reason FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row["actionable"] == 1
    assert row["actionability_reason"] == "v1_pass_core_signal_mcap_10_50m"


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
        signal_combo="gainers_early",
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
        signal_combo="volume_spike",
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
        signal_combo="volume_spike",
    )
    cursor = await db._conn.execute(
        "SELECT amount_usd FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    assert row[0] == pytest.approx(2000.0)


# ---------------------------------------------------------------------------
# GA-11: fire-and-forget TG alert tasks must log exceptions, not swallow them
# ---------------------------------------------------------------------------


async def test_tg_alert_task_exception_is_logged():
    """A failing _tg_alert_tasks task must emit paper_open_alert_task_failed."""
    import asyncio

    import structlog

    from scout.trading.engine import _log_tg_alert_task_exception

    async def _boom():
        raise RuntimeError("tg dispatch kaput")

    task = asyncio.get_event_loop().create_task(_boom())
    with pytest.raises(RuntimeError):
        await task

    with structlog.testing.capture_logs() as logs:
        _log_tg_alert_task_exception(task)

    events = [e for e in logs if e["event"] == "paper_open_alert_task_failed"]
    assert len(events) == 1
    assert "tg dispatch kaput" in events[0]["error"]


async def test_tg_alert_task_cancelled_does_not_log_or_raise():
    """Cancelled tasks are not exceptions — callback must be a no-op."""
    import asyncio

    import structlog

    from scout.trading.engine import _log_tg_alert_task_exception

    task = asyncio.get_event_loop().create_task(asyncio.sleep(30))
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    with structlog.testing.capture_logs() as logs:
        _log_tg_alert_task_exception(task)  # must not raise CancelledError

    assert not [e for e in logs if e["event"] == "paper_open_alert_task_failed"]


# ---------------------------------------------------------------------------
# SIG-10: trust-weighted paper sizing (paper-only, flag-gated, fail-closed)
#
# Resolver-level mapping/registry coverage lives in test_trust_sizing.py; these
# tests assert the engine wiring — scaling of amount_usd, the 0.0 skip + its
# decision row, and signal_data recording. Tier resolution is monkeypatched to
# a deterministic (tier, multiplier) except the one real-registry test, so the
# assertions do not couple to the committed registry's contents.
# ---------------------------------------------------------------------------


def _trust_engine(db, tmp_path, *, enabled=True, **overrides):
    s = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        DB_PATH=tmp_path / "test.db",
        TRADING_ENABLED=True,
        TRADING_MODE="paper",
        PAPER_TRADE_AMOUNT_USD=1000.0,
        PAPER_MAX_EXPOSURE_USD=100000.0,
        PAPER_TP_PCT=20.0,
        PAPER_SL_PCT=10.0,
        PAPER_SLIPPAGE_BPS=50,
        PAPER_MAX_DURATION_HOURS=48,
        PAPER_MAX_OPEN_TRADES=1000,
        PAPER_STARTUP_WARMUP_SECONDS=0,
        PAPER_TRUST_SIZING_ENABLED=enabled,
        **overrides,
    )
    return TradingEngine(mode="paper", db=db, settings=s)


async def _amount_and_signal_data(db, trade_id):
    cursor = await db._conn.execute(
        "SELECT amount_usd, signal_data FROM paper_trades WHERE id = ?",
        (trade_id,),
    )
    row = await cursor.fetchone()
    return row[0], json.loads(row[1] or "{}")


async def test_trust_sizing_off_pins_flat_amount(db, tmp_path):
    """Flag OFF (default): flat PAPER_TRADE_AMOUNT_USD, no trust keys stamped."""
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=60)
    engine = _trust_engine(db, tmp_path, enabled=False)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        signal_combo="volume_spike",
    )
    amount, signal_data = await _amount_and_signal_data(db, trade_id)
    assert amount == pytest.approx(1000.0)
    assert "trust_tier" not in signal_data
    assert "trust_size_multiplier" not in signal_data


async def test_trust_sizing_scales_amount_by_tier(db, tmp_path, monkeypatch):
    """Flag ON: notional scales by the tier multiplier; signal_data records it."""
    import scout.trading.engine as engine_mod

    monkeypatch.setattr(
        engine_mod,
        "resolve_paper_trust_size",
        lambda signal_type, settings, **kw: ("experimental", 0.5),
    )
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=60)
    engine = _trust_engine(db, tmp_path)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        signal_combo="volume_spike",
    )
    amount, signal_data = await _amount_and_signal_data(db, trade_id)
    assert amount == pytest.approx(500.0)  # 1000 * 0.5
    assert signal_data["trust_tier"] == "experimental"
    assert signal_data["trust_size_multiplier"] == 0.5


async def test_trust_sizing_real_registry_records_signal_data(db, tmp_path):
    """End-to-end via the committed registry: volume_spike -> trusted (1.0x).

    Amount is unchanged but the tier + multiplier are still recorded so
    would_be_live re-analysis can decompose by sizing policy.
    """
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=60)
    engine = _trust_engine(db, tmp_path)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        signal_combo="volume_spike",
    )
    amount, signal_data = await _amount_and_signal_data(db, trade_id)
    assert amount == pytest.approx(1000.0)
    assert signal_data["trust_tier"] == "trusted"
    assert signal_data["trust_size_multiplier"] == 1.0


async def test_trust_sizing_zero_skips_open_with_decision_row(
    db, tmp_path, monkeypatch
):
    """A 0.0 (non_tradable) multiplier skips the open and records the reason."""
    import scout.trading.engine as engine_mod

    monkeypatch.setattr(
        engine_mod,
        "resolve_paper_trust_size",
        lambda signal_type, settings, **kw: ("non_tradable", 0.0),
    )
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=60)
    engine = _trust_engine(db, tmp_path)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        signal_combo="volume_spike",
    )
    assert trade_id is None
    # No paper trade row opened.
    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE token_id = ?", ("bitcoin",)
    )
    assert (await cursor.fetchone())[0] == 0
    # A blocked decision row records the trust_sized_zero reason.
    cursor = await db._conn.execute(
        """SELECT decision, reason FROM trade_decision_events
           WHERE token_id = ? ORDER BY id DESC LIMIT 1""",
        ("bitcoin",),
    )
    row = await cursor.fetchone()
    assert row == ("blocked", "trust_sized_zero")
