"""ALR-03 engine universe-exclusion gate (scout/trading/engine.py open_trade).

The send-layer filter (scout/trading/tg_alert_dispatch._check_universe)
suppresses only the operator ALERT; the paper ENGINE still OPENS trades on
out-of-universe CoinGecko ids (tokenized equities / ETFs, e.g.
`spy-bstocks-tokenized-stock`), contaminating paper_trades and every
downstream PnL surface. This gate — fail-closed behind
ENGINE_UNIVERSE_FILTER_ENABLED — blocks the OPEN at the same single choke
point the quarantine gate uses, reusing the SAME
ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS list (one universe definition).

Ordering: the universe gate runs BEFORE the quarantine gate, so a token that
is both universe-excluded and quarantined is recorded 'universe_excluded'.

Mirrors tests/test_trading_dispatch_quarantine.py. Price is seeded so the ONLY
reason for a block is the gate under test (not a downstream no_price gate).
"""

from datetime import datetime, timedelta, timezone

from scout.config import Settings
from scout.db import Database
from scout.trading.engine import TradingEngine

_TOKENIZED_ID = "spy-bstocks-tokenized-stock"


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


async def test_flag_off_lets_tokenized_stock_open(tmp_path):
    """Flag OFF (fail-closed default): a tokenized-stock id opens normally —
    pure passthrough, pinning the pre-fix contamination behavior."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_price_cache(db, _TOKENIZED_ID, 1.0, age_seconds=60)
    engine = TradingEngine(mode="paper", db=db, settings=_make_settings())

    trade_id = await engine.open_trade(
        token_id=_TOKENIZED_ID,
        symbol="SPY",
        name="SPY tokenized",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.0},
        signal_combo="volume_spike",
    )

    assert trade_id is not None
    rows = await _decision_rows(db, "volume_spike")
    assert all(r["reason"] != "universe_excluded" for r in rows)
    await db.close()


async def test_flag_on_blocks_tokenized_stock_and_records_decision(tmp_path):
    """Flag ON: a tokenized-stock id does not open and is recorded
    decision='blocked' reason='universe_excluded' with the matched pattern."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_price_cache(db, _TOKENIZED_ID, 1.0, age_seconds=60)
    engine = TradingEngine(
        mode="paper",
        db=db,
        settings=_make_settings(ENGINE_UNIVERSE_FILTER_ENABLED=True),
    )

    trade_id = await engine.open_trade(
        token_id=_TOKENIZED_ID,
        symbol="SPY",
        name="SPY tokenized",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.0},
        signal_combo="volume_spike",
    )

    assert trade_id is None
    assert await _open_count(db, "volume_spike") == 0
    rows = await _decision_rows(db, "volume_spike")
    assert [(r["decision"], r["reason"]) for r in rows] == [
        ("blocked", "universe_excluded")
    ]
    await db.close()


async def test_flag_on_memecoin_unaffected(tmp_path):
    """Flag ON: an in-universe memecoin id opens normally."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_price_cache(db, "pepe", 0.0001, age_seconds=60)
    engine = TradingEngine(
        mode="paper",
        db=db,
        settings=_make_settings(ENGINE_UNIVERSE_FILTER_ENABLED=True),
    )

    trade_id = await engine.open_trade(
        token_id="pepe",
        symbol="PEPE",
        name="Pepe",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.0},
        signal_combo="volume_spike",
    )

    assert trade_id is not None
    rows = await _decision_rows(db, "volume_spike")
    assert all(r["reason"] != "universe_excluded" for r in rows)
    await db.close()


async def test_flag_on_quarantine_still_blocks_non_universe_token(tmp_path):
    """Flag ON: placing the universe gate before the quarantine gate does NOT
    disturb quarantine — a quarantined signal on an in-universe token is still
    recorded reason='quarantined'."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_price_cache(db, "bitcoin", 50000.0, age_seconds=60)
    engine = TradingEngine(
        mode="paper",
        db=db,
        settings=_make_settings(ENGINE_UNIVERSE_FILTER_ENABLED=True),
    )

    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="narrative_prediction",  # quarantined by default
        signal_data={"fit": 3},
        signal_combo="narrative_prediction",
    )

    assert trade_id is None
    rows = await _decision_rows(db, "narrative_prediction")
    assert [(r["decision"], r["reason"]) for r in rows] == [("blocked", "quarantined")]
    await db.close()


async def test_universe_gate_runs_before_quarantine_gate(tmp_path):
    """Ordering pin: a token that is BOTH universe-excluded AND quarantined is
    recorded 'universe_excluded' (universe gate runs first)."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await _seed_price_cache(db, _TOKENIZED_ID, 1.0, age_seconds=60)
    engine = TradingEngine(
        mode="paper",
        db=db,
        settings=_make_settings(ENGINE_UNIVERSE_FILTER_ENABLED=True),
    )

    trade_id = await engine.open_trade(
        token_id=_TOKENIZED_ID,
        symbol="SPY",
        name="SPY tokenized",
        chain="coingecko",
        signal_type="narrative_prediction",  # also quarantined
        signal_data={"fit": 3},
        signal_combo="narrative_prediction",
    )

    assert trade_id is None
    rows = await _decision_rows(db, "narrative_prediction")
    assert [(r["decision"], r["reason"]) for r in rows] == [
        ("blocked", "universe_excluded")
    ]
    await db.close()
