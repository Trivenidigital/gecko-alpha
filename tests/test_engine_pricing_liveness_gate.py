"""A position must not open when CoinGecko pricing is not LIVE.

Mandatory pre-September safety check for the 2026-08-21 monthly-budget repair.

The engine's step-0c gate (`PAPER_REQUIRE_PRICEABLE_TOKEN_ID` +
`resolve_price_source`) is a REGISTRY check: a token is admitted iff it is
CG-id-shaped ("the CG lanes serve it") OR a price_cache row exists. That proves
a source exists IN PRINCIPLE. It does not prove one can price the token TODAY,
and the premise is false whenever CG is unavailable — on 2026-08-21 the Basic
plan hit 100.0% of its 100,000 monthly credits with 11 days to the reset.

Why that is dangerous rather than merely inconvenient: `price_cache` is written
EXCLUSIVELY by CG lanes (main.py `all_raw`, narrative/agent.py,
outcome_ledger.py) — DexScreener and GeckoTerminal do NOT write it — and the
exit evaluator reads ONLY price_cache. A position opened while CG is dark is
therefore unmonitored: no trailing stop, no stop-loss, until max_duration
force-closes it at entry_price with a fabricated pnl_pct=0
(`exit_reason='expired_stale_no_price'`).
"""

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


def _settings(tmp_path, **over):
    base = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="t",
        ANTHROPIC_API_KEY="t",
        DB_PATH=tmp_path / "test.db",
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
        PAPER_OPEN_CG_PRICING_MAX_AGE_SEC=1800,
    )
    base.update(over)
    return Settings(**base)


async def _seed_price_cache(db, coin_id, price, age_seconds=0):
    ts = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d,
            market_cap, updated_at)
           VALUES (?, ?, 0, 0, 0, ?)""",
        (coin_id, price, ts.isoformat()),
    )
    await db._conn.commit()


async def _open(engine, token_id="bitcoin", entry_price=100.0):
    return await engine.open_trade(
        token_id=token_id,
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"mcap": 1_000_000},
        entry_price=entry_price,
        signal_combo="volume_spike",
    )


# ---------------------------------------------------------------------------
# The age helper
# ---------------------------------------------------------------------------


async def test_age_helper_returns_none_on_empty_cache(db, tmp_path):
    """Unknown must be distinguishable from fresh — and treated as not live."""
    engine = TradingEngine(mode="paper", db=db, settings=_settings(tmp_path))
    assert await engine._newest_price_cache_age_seconds() is None


async def test_age_helper_reads_the_freshest_row_not_an_arbitrary_one(db, tmp_path):
    """Whole-table liveness: one fresh write means the provider is alive."""
    engine = TradingEngine(mode="paper", db=db, settings=_settings(tmp_path))
    await _seed_price_cache(db, "old-coin", 1.0, age_seconds=99_000)
    await _seed_price_cache(db, "new-coin", 2.0, age_seconds=30)
    age = await engine._newest_price_cache_age_seconds()
    assert age is not None and age < 120


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


async def test_open_blocked_when_price_cache_is_entirely_empty(db, tmp_path):
    """CG never wrote anything — nothing can ever be re-priced."""
    engine = TradingEngine(mode="paper", db=db, settings=_settings(tmp_path))
    assert await _open(engine) is None


async def test_open_blocked_when_newest_write_is_older_than_the_bound(db, tmp_path):
    """THE September scenario: CG dark, cache frozen at the moment it died."""
    engine = TradingEngine(mode="paper", db=db, settings=_settings(tmp_path))
    await _seed_price_cache(db, "bitcoin", 100.0, age_seconds=7 * 24 * 3600)
    assert await _open(engine) is None


async def test_open_allowed_when_a_recent_cg_write_exists(db, tmp_path):
    """The gate must not block normal operation."""
    engine = TradingEngine(mode="paper", db=db, settings=_settings(tmp_path))
    await _seed_price_cache(db, "bitcoin", 100.0, age_seconds=60)
    assert await _open(engine) is not None


async def test_gate_applies_even_when_caller_supplies_entry_price(db, tmp_path):
    """THE BYPASS this gate exists to close.

    The pre-existing `trade_skipped_stale_price` check lives in the `else` arm
    of `if entry_price is not None`, so supplying entry_price skips it — and
    EVERY production signal in scout/trading/signals.py supplies one. A
    caller-supplied price says what the token was worth at SIGHTING; it says
    nothing about whether we can re-price it to EXIT.
    """
    engine = TradingEngine(mode="paper", db=db, settings=_settings(tmp_path))
    await _seed_price_cache(db, "bitcoin", 100.0, age_seconds=30 * 24 * 3600)
    assert await _open(engine, entry_price=123.45) is None


async def test_bound_is_settings_sourced_and_disablable(db, tmp_path):
    """0 disables the gate; the threshold is never hardcoded."""
    engine_off = TradingEngine(
        mode="paper",
        db=db,
        settings=_settings(tmp_path, PAPER_OPEN_CG_PRICING_MAX_AGE_SEC=0),
    )
    await _seed_price_cache(db, "bitcoin", 100.0, age_seconds=30 * 24 * 3600)
    assert await engine_off.open_trade(
        token_id="bitcoin", symbol="BTC", name="Bitcoin", chain="coingecko",
        signal_type="volume_spike", signal_data={"mcap": 1_000_000},
        entry_price=100.0, signal_combo="volume_spike",
    ) is not None, "gate should be disablable for an explicit operator override"


async def test_tight_bound_blocks_what_a_loose_bound_admits(db, tmp_path):
    """The decision tracks the CONFIGURED bound, not a fixed constant."""
    await _seed_price_cache(db, "bitcoin", 100.0, age_seconds=3600)

    loose = TradingEngine(
        mode="paper", db=db,
        settings=_settings(tmp_path, PAPER_OPEN_CG_PRICING_MAX_AGE_SEC=7200),
    )
    assert await _open(loose) is not None

    tight = TradingEngine(
        mode="paper", db=db,
        settings=_settings(tmp_path, PAPER_OPEN_CG_PRICING_MAX_AGE_SEC=600),
    )
    assert await _open(tight) is None
