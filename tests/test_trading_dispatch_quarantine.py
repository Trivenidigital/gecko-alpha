"""SIG-03 dispatch-quarantine gate (scout/trading/engine.py open_trade).

The gate blocks paper-trade OPENS for any signal_type in
SIGNAL_DISPATCH_QUARANTINE at the single choke point every open path converges
on (signals.py dispatchers AND scout/social/telegram/dispatcher.py both call
engine.open_trade). Detection / tracker / research surfaces are unaffected.

Placement note: the SIG-03 spec put the gate in should_open(), but tg_social
dispatches via scout/social/telegram/dispatcher.py and never calls should_open
— so a should_open gate would silently miss it. test_tg_social_dispatcher_path
_blocked covers exactly that path. Mirrors tests/test_trading_engine.py and
tests/test_tg_social_dispatcher.py.
"""

from datetime import datetime, timedelta, timezone

from scout.config import Settings
from scout.db import Database
from scout.social.telegram.dispatcher import dispatch_to_engine
from scout.social.telegram.models import ResolvedToken
from scout.trading.engine import TradingEngine


def _make_settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        TRADING_ENABLED=True,
        TRADING_MODE="paper",
        PAPER_TRADE_AMOUNT_USD=1000.0,
        PAPER_MAX_EXPOSURE_USD=5000.0,
        PAPER_TP_PCT=20.0,
        PAPER_SL_PCT=10.0,
        PAPER_SLIPPAGE_BPS=50,
        PAPER_MAX_DURATION_HOURS=48,
        PAPER_MAX_OPEN_TRADES=1000,
        PAPER_STARTUP_WARMUP_SECONDS=0,
    )
    base.update(overrides)
    return Settings(**base)


async def _seed_price_cache(db, coin_id, price, age_seconds=0):
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d, market_cap, updated_at)
           VALUES (?, ?, 0, 0, 0, ?)""",
        (coin_id, price, ts.isoformat()),
    )
    await db._conn.commit()


async def _decision_rows(db, signal_type):
    cur = await db._conn.execute(
        "SELECT decision, reason FROM trade_decision_events WHERE signal_type = ?",
        (signal_type,),
    )
    return await cur.fetchall()


async def _open_count(db, signal_type):
    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE signal_type = ? AND status = 'open'",
        (signal_type,),
    )
    row = await cur.fetchone()
    return row[0]


async def _add_tg_channel(db, handle="@gem", trade_eligible=1):
    await db._conn.execute(
        "INSERT OR REPLACE INTO tg_social_channels "
        "(channel_handle, display_name, trade_eligible, added_at) VALUES (?, ?, ?, ?)",
        (handle, "Gem", trade_eligible, datetime.now(timezone.utc).isoformat()),
    )
    await db._conn.commit()


def _resolved() -> ResolvedToken:
    return ResolvedToken(
        token_id="tok",
        symbol="TOK",
        chain="ethereum",
        contract_address="0xabc",
        mcap=1_000_000.0,
        price_usd=1.0,
        volume_24h_usd=100.0,
        safety_pass=True,
        safety_check_completed=True,
    )


async def test_quarantined_signal_blocks_open_and_records_decision(tmp_path):
    """A quarantined signal_type does not open and is recorded reason='quarantined'.

    Price is seeded so the ONLY reason for the block is the quarantine (not a
    downstream no_price / unpriceable gate).
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=60)
    engine = TradingEngine(mode="paper", db=db, settings=_make_settings())

    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="narrative_prediction",
        signal_data={"fit": 3},
        signal_combo="narrative_prediction",
    )

    assert trade_id is None
    assert await _open_count(db, "narrative_prediction") == 0
    rows = await _decision_rows(db, "narrative_prediction")
    assert [(r["decision"], r["reason"]) for r in rows] == [("blocked", "quarantined")]
    await db.close()


async def test_non_quarantined_signal_unaffected(tmp_path):
    """A signal_type NOT in the quarantine set opens normally."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=60)
    engine = TradingEngine(mode="paper", db=db, settings=_make_settings())

    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.0},
        signal_combo="volume_spike",
    )

    assert trade_id is not None
    rows = await _decision_rows(db, "volume_spike")
    assert all(r["reason"] != "quarantined" for r in rows)
    await db.close()


async def test_empty_quarantine_disables(tmp_path):
    """An empty SIGNAL_DISPATCH_QUARANTINE disables the gate — the normally
    quarantined lane opens."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=60)
    engine = TradingEngine(
        mode="paper", db=db, settings=_make_settings(SIGNAL_DISPATCH_QUARANTINE=[])
    )

    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="narrative_prediction",
        signal_data={"fit": 3},
        signal_combo="narrative_prediction",
    )

    assert trade_id is not None
    rows = await _decision_rows(db, "narrative_prediction")
    assert all(r["reason"] != "quarantined" for r in rows)
    await db.close()


async def test_tg_social_dispatcher_path_blocked(tmp_path):
    """The tg_social path — which bypasses should_open entirely — is blocked at
    engine.open_trade. This is the case the spec's should_open premise missed.

    Admission gates all pass (mirrors test_gate_all_pass_dispatches), so the
    only block is the engine-level quarantine gate.
    """
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _add_tg_channel(db)
    settings = _make_settings()  # default quarantines tg_social
    engine = TradingEngine(mode="paper", db=db, settings=settings)

    result = await dispatch_to_engine(
        db=db,
        settings=settings,
        engine=engine,
        token=_resolved(),
        channel_handle="@gem",
    )

    assert result == (None, "engine_rejected")
    assert await _open_count(db, "tg_social") == 0
    rows = await _decision_rows(db, "tg_social")
    assert [(r["decision"], r["reason"]) for r in rows] == [("blocked", "quarantined")]
    await db.close()
