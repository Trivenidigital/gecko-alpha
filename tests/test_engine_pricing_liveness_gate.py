"""A position must not open when the CoinGecko PROVIDER is not live.

Mandatory pre-September safety check for the 2026-08-21 monthly-budget repair,
corrected after review.

The engine's step-0c gate (``PAPER_REQUIRE_PRICEABLE_TOKEN_ID`` +
``resolve_price_source``) is a REGISTRY check: a token is admitted iff it is
CG-id-shaped ("the CG lanes serve it") OR a price_cache row exists. That proves
a source exists IN PRINCIPLE. It does not prove one can price the token TODAY,
and the premise is false whenever CoinGecko is unavailable -- on 2026-08-21 the
Basic plan hit 100.0% of its 100,000 monthly credits with 11 days to the reset.

Why that is dangerous rather than inconvenient: the exit evaluator re-prices
only from ``price_cache``. A position opened while CoinGecko is dark is
unmonitored -- no trailing stop, no stop-loss -- until ``max_duration``
force-closes it at ``entry_price`` with a fabricated ``pnl_pct=0``
(``exit_reason='expired_stale_no_price'``).

WHY THE FIRST VERSION OF THIS GATE WAS WRONG
--------------------------------------------
It read ``MAX(price_cache.updated_at)`` and assumed every writer was CoinGecko.
False: ``outcome_ledger._poll_dex_enrollments`` prices ``dex:`` tokens from
DEXSCREENER and writes them through ``Database.cache_prices``. So a fresh
DexScreener row made a dead CoinGecko look alive -- admitting exactly the
unpriceable position the gate exists to prevent -- and a quiet price_cache could
block a healthy Dex-backed token for an unrelated provider's silence.

Liveness is now asked of the PROVIDER (``cg_budget.last_success_at``, set only
by a CoinGecko HTTP 200) and applied only to CG-SERVED positions.
"""

from datetime import datetime, timedelta, timezone

import pytest

from scout.coingecko_budget import BUCKET_CRITICAL, BUCKET_DISCOVERY
from scout.coingecko_budget import budget as cg_budget
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


def _cg_alive(seconds_ago: float = 30) -> None:
    cg_budget.last_success_at = datetime.now(timezone.utc) - timedelta(
        seconds=seconds_ago
    )


def _cg_dead() -> None:
    cg_budget.last_success_at = None


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
# The gate
# ---------------------------------------------------------------------------


async def test_open_blocked_when_coingecko_has_never_answered(db, tmp_path):
    """Unknown is NOT live. Conflating it with fresh is how this class recurs."""
    _cg_dead()
    engine = TradingEngine(mode="paper", db=db, settings=_settings(tmp_path))
    assert await _open(engine) is None


async def test_open_blocked_when_last_cg_success_is_older_than_the_bound(db, tmp_path):
    """THE September scenario: CoinGecko refusing everything for days."""
    _cg_alive(seconds_ago=7 * 24 * 3600)
    engine = TradingEngine(mode="paper", db=db, settings=_settings(tmp_path))
    assert await _open(engine) is None


async def test_open_allowed_when_coingecko_answered_recently(db, tmp_path):
    """The gate must not block normal operation."""
    _cg_alive(seconds_ago=60)
    engine = TradingEngine(mode="paper", db=db, settings=_settings(tmp_path))
    assert await _open(engine) is not None


async def test_gate_applies_even_when_caller_supplies_entry_price(db, tmp_path):
    """THE BYPASS this gate exists to close.

    The pre-existing ``trade_skipped_stale_price`` check lives in the ``else``
    arm of ``if entry_price is not None``, so supplying entry_price skips it --
    and EVERY production signal in scout/trading/signals.py supplies one. A
    caller-supplied price says what the token was worth at SIGHTING; it says
    nothing about whether we can re-price it to EXIT.
    """
    _cg_alive(seconds_ago=30 * 24 * 3600)
    engine = TradingEngine(mode="paper", db=db, settings=_settings(tmp_path))
    assert await _open(engine, entry_price=123.45) is None


# ---------------------------------------------------------------------------
# The two-source falsifier -- the defect review found
# ---------------------------------------------------------------------------


async def test_fresh_dex_price_cache_does_NOT_admit_a_cg_token(db, tmp_path):
    """Discriminator 1. This is the exact bug the first version shipped.

    ``outcome_ledger._poll_dex_enrollments`` writes DexScreener prices through
    ``Database.cache_prices``. Under the old ``MAX(price_cache.updated_at)``
    gate this row made CoinGecko look alive and admitted a CG-only position
    nothing could re-price.
    """
    _cg_dead()
    # A DexScreener-written row, fresher than any threshold.
    await _seed_price_cache(db, "dex:solana:So11111111111111111111111111111111111111112", 1.0, age_seconds=1)
    await _seed_price_cache(db, "some-dex-token", 2.0, age_seconds=1)

    engine = TradingEngine(mode="paper", db=db, settings=_settings(tmp_path))
    assert await _open(engine) is None, (
        "a fresh DexScreener price_cache row must not present CoinGecko as live"
    )


async def test_stale_price_cache_does_NOT_block_when_coingecko_is_healthy(db, tmp_path):
    """Discriminator 2, the reverse error.

    The old gate would refuse every open whenever price_cache went quiet, even
    with CoinGecko answering perfectly -- e.g. a lull in discovery writes.
    Provider health is the question, so an empty price_cache must not block.
    """
    _cg_alive(seconds_ago=30)
    engine = TradingEngine(mode="paper", db=db, settings=_settings(tmp_path))
    # price_cache deliberately EMPTY.
    assert await _open(engine) is not None


# ---------------------------------------------------------------------------
# Critical reserve gates NEW opens (not re-pricing)
# ---------------------------------------------------------------------------


async def test_exhausted_critical_reserve_blocks_new_opens(db, tmp_path, settings_factory):
    """More open positions means more re-pricing demand against a spent reserve.

    Re-pricing itself is NOT stopped (that would recreate the fabricated close)
    -- see tests/test_coingecko_budget_enforcement.py. Taking on NEW demand is.
    """
    _cg_alive(seconds_ago=30)
    s = _settings(tmp_path, COINGECKO_MONTHLY_CRITICAL_CREDITS=2)
    for _ in range(2):
        cg_budget.record(BUCKET_CRITICAL, billable=True)
    engine = TradingEngine(mode="paper", db=db, settings=s)
    assert await _open(engine) is None


# ---------------------------------------------------------------------------
# Configurability
# ---------------------------------------------------------------------------


async def test_bound_is_settings_sourced_and_disablable(db, tmp_path):
    """0 disables the gate; the threshold is never hardcoded."""
    _cg_alive(seconds_ago=30 * 24 * 3600)
    engine_off = TradingEngine(
        mode="paper",
        db=db,
        settings=_settings(tmp_path, PAPER_OPEN_CG_PRICING_MAX_AGE_SEC=0),
    )
    assert await _open(engine_off) is not None, (
        "gate should be disablable for an explicit operator override"
    )


async def test_tight_bound_blocks_what_a_loose_bound_admits(db, tmp_path):
    """The decision tracks the CONFIGURED bound, not a fixed constant."""
    _cg_alive(seconds_ago=3600)
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


def test_only_a_billable_200_refreshes_the_heartbeat(tmp_path):
    """A 429 storm is CoinGecko REFUSING us -- the opposite of live."""
    s = _settings(tmp_path)
    _cg_dead()
    for _ in range(100):
        cg_budget.record(BUCKET_DISCOVERY, billable=False)
    assert cg_budget.cg_pricing_live(s) is False
    cg_budget.record(BUCKET_DISCOVERY, billable=True)
    assert cg_budget.cg_pricing_live(s) is True
