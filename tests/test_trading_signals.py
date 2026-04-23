"""Tests for trade_gainers / trade_losers / trade_trending dispatch filters.

These were previously dead code (not called from main.py/agent.py) so the
mcap / rank filters had zero coverage. Cover the filter branches here to
guard against regressions — specifically, NULL market_cap and below-threshold
mcap/rank must skip cleanly without raising.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.engine import TradingEngine
from scout.trading.signals import (
    trade_first_signals,
    trade_gainers,
    trade_losers,
    trade_predictions,
    trade_trending,
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
        PAPER_TRADE_AMOUNT_USD=1000.0,
        PAPER_MAX_EXPOSURE_USD=10_000.0,
        PAPER_TP_PCT=20.0,
        PAPER_SL_PCT=10.0,
        PAPER_SLIPPAGE_BPS=50,
        PAPER_MAX_DURATION_HOURS=48,
        PAPER_MIN_MCAP=5_000_000,
        PAPER_MAX_MCAP_RANK=1500,
        PAPER_MAX_OPEN_TRADES=1000,
        PAPER_STARTUP_WARMUP_SECONDS=0,
    )


@pytest.fixture
def engine(db, settings):
    return TradingEngine(mode="paper", db=db, settings=settings)


async def _insert_gainer(db, coin_id, market_cap, price=1.0):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (coin_id, coin_id.upper(), coin_id, 25.0, market_cap, 100_000.0, price, now),
    )
    await db._conn.commit()


async def _insert_loser(db, coin_id, market_cap, price=1.0):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO losers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (coin_id, coin_id.upper(), coin_id, -25.0, market_cap, 100_000.0, price, now),
    )
    await db._conn.commit()


async def _insert_trending(db, coin_id, market_cap_rank):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO trending_snapshots
           (coin_id, symbol, name, market_cap_rank, trending_score, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (coin_id, coin_id.upper(), coin_id, market_cap_rank, 1.0, now),
    )
    await db._conn.commit()


async def _seed_price(db, coin_id, price=1.0, market_cap=10_000_000):
    """Seed price_cache. Default mcap ($10M) keeps existing trending tests in-range
    after the shift from rank-proxy to real-mcap filtering in trade_trending."""
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d, market_cap, updated_at)
           VALUES (?, ?, 0, 0, ?, ?)""",
        (coin_id, price, market_cap, now),
    )
    await db._conn.commit()


async def _open_count(db):
    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE status = 'open'"
    )
    row = await cursor.fetchone()
    return row[0]


# ---------------- trade_gainers --------------------------------------------


async def test_trade_gainers_opens_trade_when_mcap_above_min(db, engine, settings):
    await _insert_gainer(db, "btc-like", market_cap=10_000_000)
    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 1


async def test_trade_gainers_skips_below_min_mcap(db, engine, settings):
    await _insert_gainer(db, "micro-cap", market_cap=1_000_000)  # below 5M floor
    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_gainers_skips_null_mcap(db, engine, settings):
    await _insert_gainer(db, "null-mcap", market_cap=None)
    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_gainers_respects_threshold_override(db, engine, settings):
    await _insert_gainer(db, "mid-cap", market_cap=2_000_000)
    # Override to $1M — should now open
    await trade_gainers(engine, db, min_mcap=1_000_000, settings=settings)
    assert await _open_count(db) == 1


# ---------------- trade_losers ---------------------------------------------


async def test_trade_losers_opens_trade_when_mcap_above_min(db, engine, settings):
    await _insert_loser(db, "btc-dip", market_cap=10_000_000)
    await trade_losers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 1


async def test_trade_losers_skips_below_min_mcap(db, engine, settings):
    await _insert_loser(db, "micro-dip", market_cap=500_000)
    await trade_losers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_losers_skips_null_mcap(db, engine, settings):
    await _insert_loser(db, "null-dip", market_cap=None)
    await trade_losers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_losers_falls_back_to_price_cache(db, engine, settings):
    """When price_at_snapshot is NULL, loader reads from price_cache."""
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO losers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("null-price", "NP", "null-price", -25.0, 10_000_000, 100_000.0, None, now),
    )
    await db._conn.commit()
    await _seed_price(db, "null-price", price=0.042)
    await trade_losers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 1


# ---------------- trade_trending -------------------------------------------


async def test_trade_trending_opens_when_rank_under_threshold(db, engine, settings):
    await _insert_trending(db, "top-100", market_cap_rank=50)
    await _seed_price(db, "top-100", price=1.0)
    await trade_trending(engine, db, max_mcap_rank=1500, settings=settings)
    assert await _open_count(db) == 1


async def test_trade_trending_skips_above_rank_threshold(db, engine, settings):
    await _insert_trending(db, "rank-2000", market_cap_rank=2000)
    await _seed_price(db, "rank-2000", price=1.0)
    await trade_trending(engine, db, max_mcap_rank=1500, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_trending_skips_null_rank(db, engine, settings):
    await _insert_trending(db, "no-rank", market_cap_rank=None)
    await _seed_price(db, "no-rank", price=1.0)
    await trade_trending(engine, db, max_mcap_rank=1500, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_trending_respects_threshold_override(db, engine, settings):
    await _insert_trending(db, "rank-1200", market_cap_rank=1200)
    await _seed_price(db, "rank-1200", price=1.0)
    # Tighter ceiling — should reject
    await trade_trending(engine, db, max_mcap_rank=1000, settings=settings)
    assert await _open_count(db) == 0


# ---------------- Datetime-window regression --------------------------------
# Bug: Stored timestamps use ISO format ('2026-04-17T06:07:17.297281+00:00')
# while SQLite's datetime('now', ...) returns space-separated form
# ('2026-04-17 06:07:17'). Raw string comparison treats 'T' (0x54) > ' ' (0x20),
# so `snapshot_at >= datetime('now', '-5 minutes')` matches ANY same-day
# snapshot, not just the last 5 minutes. This caused gainers_early to open
# with entry prices taken from early-morning peak snapshots.


async def _insert_gainer_at(db, coin_id, market_cap, price, snapshot_at):
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            coin_id,
            coin_id.upper(),
            coin_id,
            25.0,
            market_cap,
            100_000.0,
            price,
            snapshot_at,
        ),
    )
    await db._conn.commit()


async def _insert_loser_at(db, coin_id, market_cap, price, snapshot_at):
    await db._conn.execute(
        """INSERT INTO losers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            coin_id,
            coin_id.upper(),
            coin_id,
            -25.0,
            market_cap,
            100_000.0,
            price,
            snapshot_at,
        ),
    )
    await db._conn.commit()


async def _insert_trending_at(db, coin_id, market_cap_rank, snapshot_at):
    await db._conn.execute(
        """INSERT INTO trending_snapshots
           (coin_id, symbol, name, market_cap_rank, trending_score, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (coin_id, coin_id.upper(), coin_id, market_cap_rank, 1.0, snapshot_at),
    )
    await db._conn.commit()


async def test_trade_gainers_skips_snapshots_older_than_5min_same_day(
    db, engine, settings
):
    """A snapshot stored 2 hours ago (same day) must NOT be picked up."""
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await _insert_gainer_at(
        db, "stale-gainer", 10_000_000, price=1.0, snapshot_at=stale
    )
    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_losers_skips_snapshots_older_than_5min_same_day(
    db, engine, settings
):
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await _insert_loser_at(db, "stale-loser", 10_000_000, price=1.0, snapshot_at=stale)
    await trade_losers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_trending_skips_snapshots_older_than_5min_same_day(
    db, engine, settings
):
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    await _insert_trending_at(db, "stale-trend", market_cap_rank=100, snapshot_at=stale)
    await _seed_price(db, "stale-trend", price=1.0)
    await trade_trending(engine, db, max_mcap_rank=1500, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_gainers_uses_fresh_snapshot_price_not_earlier_peak(
    db, engine, settings
):
    """When both a stale and a fresh snapshot exist, entry must come from fresh one.

    Reproduces the production bug where entries were sourced from the day's
    earliest peak snapshot because DISTINCT + broken time filter returned the
    full day's rows, and the first iterated row won via engine dedup.
    """
    peak_earlier = (datetime.now(timezone.utc) - timedelta(hours=10)).isoformat()
    fresh = datetime.now(timezone.utc).isoformat()
    # Earlier peak at $1.75 (would be the stale-entry bug value)
    await _insert_gainer_at(
        db, "two-snap", 10_000_000, price=1.75, snapshot_at=peak_earlier
    )
    # Current snapshot at $1.44
    await _insert_gainer_at(db, "two-snap", 10_000_000, price=1.44, snapshot_at=fresh)
    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 1
    cur = await db._conn.execute(
        "SELECT entry_price FROM paper_trades WHERE token_id='two-snap' AND status='open'"
    )
    row = await cur.fetchone()
    entry = row[0]
    # Entry must derive from fresh $1.44 (with default 50bps slippage = $1.4472),
    # NOT from the stale $1.75 peak (which would yield ~$1.75875).
    assert entry < 1.60, f"entry {entry} came from stale snapshot, not fresh"


# ---------------- Large-cap (upper bound) paper-trade filter ---------------
# Majors (BTC, ETH, SOL, AAVE, UNI…) rarely pump fast enough to hit PAPER_TP_PCT
# within PAPER_MAX_DURATION_HOURS, so they consume slots without producing
# wins. Paper-trade admission must be gated on an upper cap, but signals/alerts
# must keep firing for these tokens — that's handled outside signals.py.


async def test_trade_gainers_skips_above_max_mcap(db, engine, settings):
    """>500M mcap must NOT open a paper trade even when above min_mcap floor."""
    await _insert_gainer(db, "big-cap-gainer", market_cap=750_000_000)
    await trade_gainers(
        engine,
        db,
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 0


async def test_trade_losers_skips_above_max_mcap(db, engine, settings):
    """>500M mcap losers are also skipped from contrarian paper trades."""
    await _insert_loser(db, "big-cap-loser", market_cap=900_000_000)
    await trade_losers(
        engine,
        db,
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 0


async def test_trade_first_signals_skips_above_max_mcap(db, engine, settings):
    """CandidateToken with mcap >500M must not open first_signal paper trade."""
    from scout.models import CandidateToken

    await _seed_price(db, "big-cap-first", price=1.0)
    token = CandidateToken(
        contract_address="big-cap-first",
        chain="coingecko",
        token_name="BigCapFirst",
        ticker="BCF",
        market_cap_usd=700_000_000,
    )
    await trade_first_signals(
        engine,
        db,
        [(token, 30, ["cg_trending_rank"])],
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 0


async def test_trade_trending_skips_above_max_mcap(db, engine, settings):
    """Major with mcap >500M must not open trending_catch paper trade.

    Uses price_cache.market_cap rather than rank proxy — same gate as the
    other 4 signal types, for consistency. The ~$5M floor and ~$500M ceiling
    apply to trending the same way they apply to gainers/losers/predictions.
    """
    await _insert_trending(db, "big-cap-trend", market_cap_rank=50)
    # price_cache.market_cap = 800M (above 500M ceiling)
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d,
            market_cap, updated_at)
           VALUES (?, 1.0, 0, 0, 800000000, ?)""",
        ("big-cap-trend", now),
    )
    await db._conn.commit()
    await trade_trending(
        engine,
        db,
        max_mcap_rank=1500,
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 0


async def test_trade_trending_skips_below_min_mcap(db, engine, settings):
    """Trending token with mcap <5M must not open (micro-cap junk floor)."""
    await _insert_trending(db, "tiny-trend", market_cap_rank=800)
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d,
            market_cap, updated_at)
           VALUES (?, 1.0, 0, 0, 1000000, ?)""",
        ("tiny-trend", now),
    )
    await db._conn.commit()
    await trade_trending(
        engine,
        db,
        max_mcap_rank=1500,
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 0


async def test_trade_trending_opens_when_mcap_in_range(db, engine, settings):
    """Mid-cap trending token (mcap ~50M, rank ~500) must open the trade."""
    await _insert_trending(db, "mid-trend", market_cap_rank=500)
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d,
            market_cap, updated_at)
           VALUES (?, 1.0, 0, 0, 50000000, ?)""",
        ("mid-trend", now),
    )
    await db._conn.commit()
    await trade_trending(
        engine,
        db,
        max_mcap_rank=1500,
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 1


async def test_trade_predictions_skips_above_max_mcap(db, engine, settings):
    """NarrativePrediction with mcap >500M must not open paper trade."""
    from datetime import datetime, timezone

    from scout.narrative.models import NarrativePrediction

    await _seed_price(db, "big-cap-pred", price=1.0)
    pred = NarrativePrediction(
        category_id="cat",
        category_name="Layer 1 (L1)",
        coin_id="big-cap-pred",
        symbol="BCP",
        name="BigCapPred",
        market_cap_at_prediction=800_000_000,
        price_at_prediction=1.0,
        narrative_fit_score=80,
        staying_power="high",
        confidence="high",
        reasoning="r",
        market_regime="bull",
        trigger_count=3,
        strategy_snapshot={},
        predicted_at=datetime.now(timezone.utc),
    )
    await trade_predictions(
        engine,
        db,
        prediction_models=[pred],
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 0


# ---------------- trade_predictions junk filters ---------------------------


def _make_pred(
    coin_id: str,
    category_name: str,
    *,
    mcap: float = 50_000_000,
    fit: int = 80,
):
    from scout.narrative.models import NarrativePrediction

    return NarrativePrediction(
        category_id="cat",
        category_name=category_name,
        coin_id=coin_id,
        symbol=coin_id.upper(),
        name=coin_id,
        market_cap_at_prediction=mcap,
        price_at_prediction=1.0,
        narrative_fit_score=fit,
        staying_power="high",
        confidence="high",
        reasoning="r",
        market_regime="bull",
        trigger_count=3,
        strategy_snapshot={},
        predicted_at=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    "category_name",
    [
        "Bridged-Tokens",
        "bridged-tokens",
        "Bridged Tokens",
        "Bridged Stablecoin",
        "Wrapped-Tokens",
        "Stock market-themed",
        "MetaDAO Launchpad",
        "Desci Meme",
        "Music",
        "Airdropped Tokens by NFT Projects",
        "Trading Card RWA Platform",
        "Murad Picks",
    ],
)
async def test_trade_predictions_skips_junk_category(
    db, engine, settings, category_name
):
    """Junk CoinGecko categories (hyphenated or spaced) must not open trades."""
    await _seed_price(db, "junk-cat-coin", price=1.0)
    pred = _make_pred("junk-cat-coin", category_name)
    await trade_predictions(
        engine,
        db,
        prediction_models=[pred],
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 0, f"Junk category {category_name!r} opened a trade"


@pytest.mark.parametrize(
    "coin_id",
    [
        "bridged-usd-coin-starkgate",
        "sui-bridged-wbtc-sui",
        "superbridge-bridged-wsteth-optimism",
        "wrapped-bitcoin",
        "arbitrum-bridged-usdc",
        "optimism-bridged-weth",
    ],
)
async def test_trade_predictions_skips_junk_coinid(db, engine, settings, coin_id):
    """Wrapped/bridged tokens must be blocked by coin_id pattern regardless of category."""
    await _seed_price(db, coin_id, price=1.0)
    pred = _make_pred(coin_id, category_name="Layer 1 (L1)")
    await trade_predictions(
        engine,
        db,
        prediction_models=[pred],
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 0, f"Bridged/wrapped coin_id {coin_id!r} passed"


async def test_trade_predictions_allows_legit_coinid_with_bridge_substring(
    db, engine, settings
):
    """Coin IDs that merely contain 'bridge' but aren't bridged/wrapped assets pass."""
    await _seed_price(db, "bridgelink", price=1.0)
    pred = _make_pred("bridgelink", category_name="AI")
    await trade_predictions(
        engine,
        db,
        prediction_models=[pred],
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 1


# ---------------- BL-059: _is_tradeable_candidate helper -------------------


@pytest.mark.parametrize(
    "coin_id,ticker,expected",
    [
        # Clean ASCII cases pass
        ("some-alt", "ALT", True),
        ("bridgelink", "BRL", True),  # 'bridge' substring that isn't a bridged asset
        ("airbender", "AIR", True),
        # Wrapped/bridged coin_id patterns fail
        ("wrapped-bitcoin", "WBTC", False),
        ("bridged-usd-coin", "USDC", False),
        ("superbridge-weth", "WETH", False),
        ("arbitrum-bridged-usdc", "USDC", False),
        ("sui-bridged-wbtc", "WBTC", False),
        # Non-ASCII ticker fails (Chinese memes — the observed leak)
        ("woo-ta-ma", "我踏马来了", False),
        ("bianrensheng", "币安人生", False),
        # Cyrillic and emoji tickers fail too
        ("some-russ", "РУБЛЬ", False),
        ("emoji-coin", "🚀", False),
        # Non-ASCII coin_id fails too (ASCII-ticker bypass — C3)
        ("我踏马来了", "BTC", False),
        ("币安人生", "ALT", False),
        ("РУБЛЬ", "RUB", False),
        ("🚀coin", "MOON", False),
        # Empty/None safe — should not crash, treat as not-tradeable
        ("", "ABC", False),
        ("some-alt", "", False),
        ("some-alt", None, False),
        (None, "ABC", False),
        # Non-str types must not crash (S4 defense-in-depth)
        (123, "ABC", False),
        ("some-alt", 456, False),
    ],
)
def test_is_tradeable_candidate(coin_id, ticker, expected):
    from scout.trading.signals import _is_tradeable_candidate

    assert _is_tradeable_candidate(coin_id, ticker) is expected


def test_is_junk_coinid_safe_on_non_str():
    """S4: non-str input must return False rather than crash."""
    from scout.trading.signals import _is_junk_coinid

    assert _is_junk_coinid(None) is False
    assert _is_junk_coinid(123) is False
    assert _is_junk_coinid("") is False
    assert _is_junk_coinid("wrapped-btc") is True


# ---------------- BL-059: filter applied to 6 trade_* entry points ---------


async def test_trade_gainers_skips_wrapped_coinid(db, engine, settings):
    await _insert_gainer(db, "wrapped-bitcoin", market_cap=10_000_000)
    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_gainers_skips_non_ascii_ticker(db, engine, settings):
    """Non-ASCII ticker (Chinese meme) must not open a trade."""
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "wo-ta-ma-laile",
            "我踏马来了",
            "我踏马来了",
            25.0,
            10_000_000,
            100.0,
            1.0,
            now,
        ),
    )
    await db._conn.commit()
    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_losers_skips_wrapped_coinid(db, engine, settings):
    await _insert_loser(db, "wrapped-bitcoin", market_cap=10_000_000)
    await trade_losers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_losers_skips_non_ascii_ticker(db, engine, settings):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO losers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        ("bianrensheng", "币安人生", "币安人生", -25.0, 10_000_000, 100.0, 1.0, now),
    )
    await db._conn.commit()
    await trade_losers(engine, db, min_mcap=5_000_000, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_trending_skips_wrapped_coinid(db, engine, settings):
    await _insert_trending(db, "wrapped-bitcoin", market_cap_rank=100)
    await _seed_price(db, "wrapped-bitcoin", price=1.0, market_cap=50_000_000)
    await trade_trending(
        engine,
        db,
        max_mcap_rank=1500,
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 0


async def test_trade_trending_skips_non_ascii_ticker(db, engine, settings):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO trending_snapshots
           (coin_id, symbol, name, market_cap_rank, trending_score, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("meme-cn", "我踏马来了", "我踏马来了", 100, 1.0, now),
    )
    await db._conn.commit()
    await _seed_price(db, "meme-cn", price=1.0, market_cap=50_000_000)
    await trade_trending(
        engine,
        db,
        max_mcap_rank=1500,
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 0


def _make_candidate(contract_address: str, ticker: str, *, mcap: float = 50_000_000):
    from scout.models import CandidateToken

    return CandidateToken(
        contract_address=contract_address,
        chain="coingecko",
        token_name=contract_address,
        ticker=ticker,
        market_cap_usd=mcap,
    )


async def test_trade_first_signals_skips_wrapped_coinid(db, engine, settings):
    token = _make_candidate("wrapped-bitcoin", "WBTC")
    await _seed_price(db, "wrapped-bitcoin", price=1.0)
    await trade_first_signals(
        engine,
        db,
        scored_candidates=[(token, 50, ["momentum_ratio"])],
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 0


async def test_trade_first_signals_skips_non_ascii_ticker(db, engine, settings):
    token = _make_candidate("wo-ta-ma-laile", "我踏马来了")
    await _seed_price(db, "wo-ta-ma-laile", price=1.0)
    await trade_first_signals(
        engine,
        db,
        scored_candidates=[(token, 50, ["momentum_ratio"])],
        min_mcap=5_000_000,
        max_mcap=500_000_000,
        settings=settings,
    )
    assert await _open_count(db) == 0


async def test_trade_chain_completions_skips_wrapped_coinid(db, engine, settings):
    """chain_matches has no symbol column — only coin_id filter applies here."""
    from scout.trading.signals import trade_chain_completions

    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_patterns
           (id, name, description, steps_json, min_steps_to_trigger)
           VALUES (1, 'p', 'd', '[]', 1)""",
    )
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost)
           VALUES (?, 'coingecko', 1, 'p', 3, 3, ?, ?, 1.0, 10)""",
        ("wrapped-bitcoin", now, now),
    )
    await db._conn.commit()
    await _seed_price(db, "wrapped-bitcoin", price=1.0)
    await trade_chain_completions(engine, db, settings=settings)
    assert await _open_count(db) == 0


async def test_trade_volume_spikes_skips_wrapped_coinid(db, engine, settings):
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from scout.spikes.models import VolumeSpike
    from scout.trading.signals import trade_volume_spikes

    spike = VolumeSpike(
        coin_id="wrapped-bitcoin",
        symbol="WBTC",
        name="Wrapped Bitcoin",
        current_volume=100.0,
        avg_volume_7d=10.0,
        spike_ratio=10.0,
        market_cap=10_000_000,
        price=1.0,
        detected_at=_dt.now(_tz.utc),
    )
    await trade_volume_spikes(engine, db, [spike], settings=settings)
    assert await _open_count(db) == 0


async def test_trade_volume_spikes_skips_non_ascii_ticker(db, engine, settings):
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from scout.spikes.models import VolumeSpike
    from scout.trading.signals import trade_volume_spikes

    spike = VolumeSpike(
        coin_id="bianrensheng",
        symbol="币安人生",
        name="币安人生",
        current_volume=100.0,
        avg_volume_7d=10.0,
        spike_ratio=10.0,
        market_cap=10_000_000,
        price=1.0,
        detected_at=_dt.now(_tz.utc),
    )
    await trade_volume_spikes(engine, db, [spike], settings=settings)
    assert await _open_count(db) == 0


# ---------------- BL-059 review: C1 positive path + S3 happy path -----------


async def test_trade_volume_spikes_opens_trade_for_clean_spike(db, engine, settings):
    """C1: clean VolumeSpike must open a trade.

    This test fails against PR #45 as first committed because the body of
    ``trade_volume_spikes`` used dict subscript/``.get`` access on the Pydantic
    ``VolumeSpike``, which raises ``TypeError`` on the first iteration. The
    broad ``except Exception`` swallowed the crash, so no trade was opened and
    no test caught the regression.
    """
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from scout.spikes.models import VolumeSpike
    from scout.trading.signals import trade_volume_spikes

    spike = VolumeSpike(
        coin_id="clean-alt",
        symbol="CALT",
        name="Clean Alt",
        current_volume=500.0,
        avg_volume_7d=50.0,
        spike_ratio=10.0,
        market_cap=10_000_000,
        price=1.25,
        detected_at=_dt.now(_tz.utc),
    )
    await trade_volume_spikes(engine, db, [spike], settings=settings)
    assert await _open_count(db) == 1


async def test_trade_chain_completions_opens_trade_for_clean_token(
    db, engine, settings
):
    """S3: a clean (ASCII, non-wrapped) token_id must open a chain trade."""
    from scout.trading.signals import trade_chain_completions

    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_patterns
           (id, name, description, steps_json, min_steps_to_trigger)
           VALUES (1, 'p', 'd', '[]', 1)""",
    )
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost)
           VALUES (?, 'coingecko', 1, 'p', 3, 3, ?, ?, 1.0, 10)""",
        ("clean-alt", now, now),
    )
    await db._conn.commit()
    await _seed_price(db, "clean-alt", price=1.0)
    await trade_chain_completions(engine, db, settings=settings)
    assert await _open_count(db) == 1


# ---------------- BL-059 review: S1 log-fire assertions --------------------
#
# Each dispatcher must emit a ``signal_skipped_junk`` event when it rejects a
# candidate, so an operator watching logs can see paper-trade admission is
# working. These tests use ``structlog.testing.capture_logs`` to prove the
# event fires with the right ``signal_type`` — a silent filter is a broken
# filter, even if _open_count stays at 0 for the right reason.


def _junk_events(cap):
    return [e for e in cap if e.get("event") == "signal_skipped_junk"]


async def test_trade_gainers_logs_signal_skipped_junk(db, engine, settings):
    import structlog.testing

    await _insert_gainer(db, "wrapped-bitcoin", market_cap=10_000_000)
    with structlog.testing.capture_logs() as cap:
        await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)
    events = _junk_events(cap)
    assert len(events) == 1
    assert events[0]["signal_type"] == "gainers_early"
    assert events[0]["coin_id"] == "wrapped-bitcoin"


async def test_trade_losers_logs_signal_skipped_junk(db, engine, settings):
    import structlog.testing

    await _insert_loser(db, "wrapped-bitcoin", market_cap=10_000_000)
    with structlog.testing.capture_logs() as cap:
        await trade_losers(engine, db, min_mcap=5_000_000, settings=settings)
    events = _junk_events(cap)
    assert len(events) == 1
    assert events[0]["signal_type"] == "losers_contrarian"


async def test_trade_trending_logs_signal_skipped_junk(db, engine, settings):
    import structlog.testing

    await _insert_trending(db, "wrapped-bitcoin", market_cap_rank=100)
    await _seed_price(db, "wrapped-bitcoin", price=1.0, market_cap=10_000_000)
    with structlog.testing.capture_logs() as cap:
        await trade_trending(engine, db, settings=settings)
    events = _junk_events(cap)
    assert len(events) == 1
    assert events[0]["signal_type"] == "trending_catch"


async def test_trade_first_signals_logs_signal_skipped_junk(db, engine, settings):
    import structlog.testing

    from scout.models import CandidateToken

    token = CandidateToken(
        chain="coingecko",
        contract_address="wrapped-bitcoin",
        ticker="WBTC",
        token_name="Wrapped Bitcoin",
        market_cap_usd=10_000_000,
    )
    with structlog.testing.capture_logs() as cap:
        await trade_first_signals(
            engine,
            db,
            [(token, 50, ["momentum_ratio"])],
            min_mcap=5_000_000,
            settings=settings,
        )
    events = _junk_events(cap)
    assert len(events) == 1
    assert events[0]["signal_type"] == "first_signal"


async def test_trade_chain_completions_logs_signal_skipped_junk(db, engine, settings):
    import structlog.testing

    from scout.trading.signals import trade_chain_completions

    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO chain_patterns
           (id, name, description, steps_json, min_steps_to_trigger)
           VALUES (1, 'p', 'd', '[]', 1)""",
    )
    await db._conn.execute(
        """INSERT INTO chain_matches
           (token_id, pipeline, pattern_id, pattern_name, steps_matched,
            total_steps, anchor_time, completed_at, chain_duration_hours,
            conviction_boost)
           VALUES (?, 'coingecko', 1, 'p', 3, 3, ?, ?, 1.0, 10)""",
        ("wrapped-bitcoin", now, now),
    )
    await db._conn.commit()
    with structlog.testing.capture_logs() as cap:
        await trade_chain_completions(engine, db, settings=settings)
    events = _junk_events(cap)
    assert len(events) == 1
    assert events[0]["signal_type"] == "chain_completed"


async def test_trade_volume_spikes_logs_signal_skipped_junk(db, engine, settings):
    import structlog.testing
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from scout.spikes.models import VolumeSpike
    from scout.trading.signals import trade_volume_spikes

    spike = VolumeSpike(
        coin_id="wrapped-bitcoin",
        symbol="WBTC",
        name="Wrapped Bitcoin",
        current_volume=100.0,
        avg_volume_7d=10.0,
        spike_ratio=10.0,
        market_cap=10_000_000,
        price=1.0,
        detected_at=_dt.now(_tz.utc),
    )
    with structlog.testing.capture_logs() as cap:
        await trade_volume_spikes(engine, db, [spike], settings=settings)
    events = _junk_events(cap)
    assert len(events) == 1
    assert events[0]["signal_type"] == "volume_spike"
