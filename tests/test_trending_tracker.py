"""Tests for trending tracker core logic (fetch, compare, stats)."""

import re
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.db import Database
from scout.ratelimit import coingecko_limiter
from scout.trending.tracker import (
    compare_with_signals,
    fetch_and_store_trending,
    get_recent_comparisons,
    get_recent_snapshots,
    get_trending_stats,
)

CG_TRENDING_URL = re.compile(r"https://api\.coingecko\.com/api/v3/search/trending")

TRENDING_RESPONSE = {
    "coins": [
        {
            "item": {
                "id": f"coin-{i}",
                "symbol": f"C{i}",
                "name": f"Coin {i}",
                "market_cap_rank": 100 + i,
            }
        }
        for i in range(5)
    ]
}


@pytest.fixture(autouse=True)
async def _clear_rate_limit():
    await coingecko_limiter.reset()
    yield
    await coingecko_limiter.reset()


@pytest.fixture
async def db(tmp_path):
    """Fresh DB with all tables."""
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


# ---------------------------------------------------------------------------
# fetch_and_store_trending
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_and_store_trending_success(db):
    """Fetches trending coins and stores them as snapshots."""
    with aioresponses() as mocked:
        mocked.get(CG_TRENDING_URL, payload=TRENDING_RESPONSE)
        async with aiohttp.ClientSession() as session:
            snapshots = await fetch_and_store_trending(session, db)

    assert len(snapshots) == 5
    assert snapshots[0].coin_id == "coin-0"
    assert snapshots[0].symbol == "C0"
    assert snapshots[0].trending_score == 1.0  # rank 1

    # Verify stored in DB
    cursor = await db._conn.execute("SELECT COUNT(*) FROM trending_snapshots")
    count = (await cursor.fetchone())[0]
    assert count == 5


@pytest.mark.asyncio
async def test_fetch_and_store_trending_empty_response(db):
    """Returns empty list on failed/empty response."""
    with aioresponses() as mocked:
        mocked.get(CG_TRENDING_URL, status=500)
        async with aiohttp.ClientSession() as session:
            snapshots = await fetch_and_store_trending(session, db)

    assert snapshots == []


@pytest.mark.asyncio
async def test_fetch_and_store_trending_malformed(db):
    """Handles malformed response gracefully."""
    with aioresponses() as mocked:
        mocked.get(CG_TRENDING_URL, payload={"coins": [{"item": {}}]})
        async with aiohttp.ClientSession() as session:
            snapshots = await fetch_and_store_trending(session, db)

    # Entry without id is skipped
    assert len(snapshots) == 0


@pytest.mark.asyncio
async def test_fetch_and_store_trending_with_api_key(db):
    """API key is passed as query param."""
    with aioresponses() as mocked:
        mocked.get(CG_TRENDING_URL, payload=TRENDING_RESPONSE)
        async with aiohttp.ClientSession() as session:
            snapshots = await fetch_and_store_trending(session, db, api_key="test-key")

    assert len(snapshots) == 5


# ---------------------------------------------------------------------------
# compare_with_signals
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_no_trending_data(db):
    """Returns empty when no trending snapshots exist."""
    comparisons = await compare_with_signals(db)
    assert comparisons == []


@pytest.mark.asyncio
async def test_compare_all_gaps(db):
    """All tokens are gaps when no matching predictions/candidates exist."""
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
        ("coin-x", "CX", "Coin X", now.isoformat()),
    )
    await db._conn.commit()

    comparisons = await compare_with_signals(db)
    assert len(comparisons) == 1
    assert comparisons[0].is_gap is True
    assert comparisons[0].coin_id == "coin-x"


@pytest.mark.asyncio
async def test_compare_detects_pipeline_candidate(db):
    """Detects a token that was in candidates before it trended."""
    now = datetime.now(timezone.utc)
    earlier = now - timedelta(hours=2)

    # Insert a candidate that was seen 2 hours ago
    await db._conn.execute(
        """INSERT INTO candidates (contract_address, chain, token_name, ticker, first_seen_at)
           VALUES (?, ?, ?, ?, ?)""",
        ("coin-a", "solana", "Coin A", "CA", earlier.isoformat()),
    )

    # Insert trending snapshot for now
    await db._conn.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
        ("coin-a", "CA", "Coin A", now.isoformat()),
    )
    await db._conn.commit()

    comparisons = await compare_with_signals(db)
    assert len(comparisons) == 1
    comp = comparisons[0]
    assert comp.is_gap is False
    assert comp.detected_by_pipeline is True
    assert comp.pipeline_lead_minutes is not None
    assert comp.pipeline_lead_minutes > 0  # we saw it earlier


@pytest.mark.asyncio
async def test_compare_detects_narrative_prediction(db):
    """Detects a token that was predicted by narrative agent before trending."""
    now = datetime.now(timezone.utc)
    earlier = now - timedelta(hours=3)

    # Insert a prediction
    await db._conn.execute(
        """INSERT INTO predictions
           (category_id, category_name, coin_id, symbol, name,
            market_cap_at_prediction, price_at_prediction,
            narrative_fit_score, staying_power, confidence, reasoning,
            market_regime, trigger_count, strategy_snapshot, predicted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        ("cat1", "Meme", "coin-b", "CB", "Coin B",
         100000, 0.5, 80, "strong", "high", "good fit",
         "BULL", 2, "{}", earlier.isoformat()),
    )

    # Insert trending snapshot
    await db._conn.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
        ("coin-b", "CB", "Coin B", now.isoformat()),
    )
    await db._conn.commit()

    comparisons = await compare_with_signals(db)
    assert len(comparisons) == 1
    comp = comparisons[0]
    assert comp.detected_by_narrative is True
    assert comp.narrative_lead_minutes > 0
    assert comp.is_gap is False


@pytest.mark.asyncio
async def test_compare_detects_chain_signal(db):
    """Detects a token seen in signal_events before trending."""
    now = datetime.now(timezone.utc)
    earlier = now - timedelta(hours=1)

    # Insert a signal event
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("coin-c", "memecoin", "candidate_scored", '{"quant_score": 75}',
         "scorer", earlier.isoformat()),
    )

    # Insert trending snapshot
    await db._conn.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
        ("coin-c", "CC", "Coin C", now.isoformat()),
    )
    await db._conn.commit()

    comparisons = await compare_with_signals(db)
    assert len(comparisons) == 1
    comp = comparisons[0]
    assert comp.detected_by_chains is True
    assert comp.chains_lead_minutes > 0
    assert comp.is_gap is False


@pytest.mark.asyncio
async def test_compare_multiple_detections(db):
    """A token detected by both pipeline and chains."""
    now = datetime.now(timezone.utc)
    earlier_pipeline = now - timedelta(hours=4)
    earlier_chain = now - timedelta(hours=2)

    await db._conn.execute(
        """INSERT INTO candidates (contract_address, chain, token_name, ticker, first_seen_at)
           VALUES (?, ?, ?, ?, ?)""",
        ("coin-d", "solana", "Coin D", "CD", earlier_pipeline.isoformat()),
    )
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("coin-d", "memecoin", "candidate_scored", '{}', "scorer", earlier_chain.isoformat()),
    )
    await db._conn.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
        ("coin-d", "CD", "Coin D", now.isoformat()),
    )
    await db._conn.commit()

    comparisons = await compare_with_signals(db)
    assert len(comparisons) == 1
    comp = comparisons[0]
    assert comp.detected_by_pipeline is True
    assert comp.detected_by_chains is True
    assert comp.is_gap is False


@pytest.mark.asyncio
async def test_compare_replaces_old_comparison(db):
    """Running compare twice replaces old comparison for same coin."""
    now = datetime.now(timezone.utc)
    await db._conn.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
        ("coin-e", "CE", "Coin E", now.isoformat()),
    )
    await db._conn.commit()

    await compare_with_signals(db)
    await compare_with_signals(db)

    cursor = await db._conn.execute(
        "SELECT COUNT(*) FROM trending_comparisons WHERE coin_id = 'coin-e'"
    )
    count = (await cursor.fetchone())[0]
    assert count == 1  # not duplicated


# ---------------------------------------------------------------------------
# get_trending_stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats_empty(db):
    """Stats on empty DB returns zeroes."""
    stats = await get_trending_stats(db)
    assert stats.total_tracked == 0
    assert stats.hit_rate_pct == 0.0
    assert stats.avg_lead_minutes is None


@pytest.mark.asyncio
async def test_stats_with_data(db):
    """Stats computed correctly from comparison data."""
    # Insert 3 comparisons: 2 caught, 1 gap
    now = datetime.now(timezone.utc)
    earlier = now - timedelta(hours=2)

    for coin_id, is_gap, narrative, lead in [
        ("a", 0, 1, 60.0),
        ("b", 0, 0, None),
        ("c", 1, 0, None),
    ]:
        pipeline = 1 if coin_id == "b" else 0
        pipeline_lead = 120.0 if coin_id == "b" else None
        await db._conn.execute(
            """INSERT INTO trending_comparisons
               (coin_id, symbol, name, appeared_on_trending_at, is_gap,
                detected_by_narrative, narrative_lead_minutes,
                detected_by_pipeline, pipeline_lead_minutes,
                detected_by_chains)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (coin_id, coin_id.upper(), f"Coin {coin_id}",
             now.isoformat(), is_gap,
             narrative, lead,
             pipeline, pipeline_lead,
             0),
        )
    await db._conn.commit()

    stats = await get_trending_stats(db)
    assert stats.total_tracked == 3
    assert stats.caught_before_trending == 2
    assert stats.missed == 1
    assert stats.hit_rate_pct == pytest.approx(66.7, abs=0.1)
    assert stats.avg_lead_minutes is not None
    assert stats.by_narrative == 1
    assert stats.by_pipeline == 1


# ---------------------------------------------------------------------------
# get_recent_snapshots / get_recent_comparisons
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recent_snapshots(db):
    """Returns recent snapshots ordered by time."""
    now = datetime.now(timezone.utc)
    for i in range(3):
        await db._conn.execute(
            "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
            (f"coin-{i}", f"C{i}", f"Coin {i}", (now - timedelta(hours=i)).isoformat()),
        )
    await db._conn.commit()

    results = await get_recent_snapshots(db, hours=24, limit=10)
    assert len(results) == 3
    # Most recent first
    assert results[0]["coin_id"] == "coin-0"


@pytest.mark.asyncio
async def test_recent_comparisons(db):
    """Returns recent comparisons ordered by trending time."""
    now = datetime.now(timezone.utc)
    for i in range(2):
        await db._conn.execute(
            """INSERT INTO trending_comparisons
               (coin_id, symbol, name, appeared_on_trending_at, is_gap)
               VALUES (?, ?, ?, ?, ?)""",
            (f"coin-{i}", f"C{i}", f"Coin {i}",
             (now - timedelta(hours=i)).isoformat(), 1),
        )
    await db._conn.commit()

    results = await get_recent_comparisons(db, limit=10)
    assert len(results) == 2
    assert results[0]["coin_id"] == "coin-0"
