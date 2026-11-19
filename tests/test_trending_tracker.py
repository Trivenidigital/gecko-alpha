"""Tests for trending tracker core logic (fetch, compare, stats)."""

import re
from datetime import datetime, timedelta, timezone


def _budget_settings():
    """Minimal Settings for the CoinGecko budget governor.

    fetch_and_store_trending now REQUIRES settings (keyword-only, no default):
    a `settings=None` default failed CLOSED but SILENTLY, returning zero
    snapshots instead of raising — which is how an unenforced call looks exactly
    like a quiet upstream.
    """
    from scout.config import Settings

    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        COINGECKO_DISCOVERY_ENABLED=True,
    )


def _sqlite_ts(dt: datetime) -> str:
    """Format datetime as SQLite-compatible string (space separator, no tz)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


import aiohttp
import pytest
from aioresponses import aioresponses

from scout.db import Database
from scout.ratelimit import coingecko_limiter
from scout.models import CandidateToken
from scout.trending.tracker import (
    _parse_dt,
    compare_with_signals,
    fetch_and_store_trending,
    get_recent_comparisons,
    get_recent_snapshots,
    get_trending_stats,
    store_trending_from_candidates,
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
            snapshots = await fetch_and_store_trending(
                session, db, settings=_budget_settings()
            )

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
            snapshots = await fetch_and_store_trending(
                session, db, settings=_budget_settings()
            )

    assert snapshots == []


@pytest.mark.asyncio
async def test_fetch_and_store_trending_malformed(db):
    """Handles malformed response gracefully."""
    with aioresponses() as mocked:
        mocked.get(CG_TRENDING_URL, payload={"coins": [{"item": {}}]})
        async with aiohttp.ClientSession() as session:
            snapshots = await fetch_and_store_trending(
                session, db, settings=_budget_settings()
            )

    # Entry without id is skipped
    assert len(snapshots) == 0


@pytest.mark.asyncio
async def test_fetch_and_store_trending_with_api_key(db):
    """API key is passed as query param."""
    with aioresponses() as mocked:
        mocked.get(CG_TRENDING_URL, payload=TRENDING_RESPONSE)
        async with aiohttp.ClientSession() as session:
            snapshots = await fetch_and_store_trending(
                session, db, api_key="test-key", settings=_budget_settings()
            )

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
        ("coin-a", "solana", "Coin A", "CA", _sqlite_ts(earlier)),
    )

    # Insert trending snapshot for now
    await db._conn.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
        ("coin-a", "CA", "Coin A", _sqlite_ts(now)),
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
        (
            "cat1",
            "Meme",
            "coin-b",
            "CB",
            "Coin B",
            100000,
            0.5,
            80,
            "strong",
            "high",
            "good fit",
            "BULL",
            2,
            "{}",
            _sqlite_ts(earlier),
        ),
    )

    # Insert trending snapshot
    await db._conn.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
        ("coin-b", "CB", "Coin B", _sqlite_ts(now)),
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
        (
            "coin-c",
            "memecoin",
            "candidate_scored",
            '{"quant_score": 75}',
            "scorer",
            _sqlite_ts(earlier),
        ),
    )

    # "CC" is a SHORT symbol, so this exercises the branch that now reads the
    # derived substrate. emit_event writes both rows as one unit; a raw INSERT
    # alone builds a state production does not produce.
    await db.record_signal_first_seen("coin-c", _sqlite_ts(earlier))

    # Insert trending snapshot
    await db._conn.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
        ("coin-c", "CC", "Coin C", _sqlite_ts(now)),
    )
    await db._conn.commit()

    comparisons = await compare_with_signals(db)
    assert len(comparisons) == 1
    comp = comparisons[0]
    assert comp.detected_by_chains is True
    assert comp.chains_lead_minutes > 0
    assert comp.is_gap is False


@pytest.mark.asyncio
async def test_compare_credits_same_day_isoformat_pipeline(db):
    """Regression (_check_detector helper, line ~63): production stores
    candidates.first_seen_at + trending snapshot_at via isoformat() ('..T..').
    A bare `<` against datetime()'s space-format dropped same-day detections
    ('T' 0x54 > ' ' 0x20). The space-format _sqlite_ts() tests above never hit
    this path; this one does."""
    now = datetime.now(timezone.utc)
    earlier = now - timedelta(hours=2)
    await db._conn.execute(
        """INSERT INTO candidates (contract_address, chain, token_name, ticker, first_seen_at)
           VALUES (?, ?, ?, ?, ?)""",
        ("coin-isop", "solana", "Coin IsoP", "CISOP", earlier.isoformat()),
    )
    await db._conn.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
        ("coin-isop", "CISOP", "Coin IsoP", now.isoformat()),
    )
    await db._conn.commit()

    comps = await compare_with_signals(db)
    assert len(comps) == 1
    assert comps[0].detected_by_pipeline is True
    assert comps[0].pipeline_lead_minutes == pytest.approx(120.0, abs=1.0)
    assert comps[0].is_gap is False


@pytest.mark.asyncio
async def test_compare_credits_same_day_isoformat_chain_signal(db):
    """Regression (inline signal_events branch, line ~266): same-day chain
    signal in isoformat-T must be credited (symbol >= 4 chars -> LIKE branch)."""
    now = datetime.now(timezone.utc)
    earlier = now - timedelta(hours=2)
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "coin-isoc",
            "memecoin",
            "candidate_scored",
            '{"quant_score": 75}',
            "scorer",
            earlier.isoformat(),
        ),
    )
    # `emit_event` writes the event AND folds signal_first_seen in one
    # transaction, and the tracker now derives first-seen from that substrate.
    # A raw INSERT alone builds a state production cannot reach (the savepoint
    # in emit_event makes an event without its substrate row unreachable), so
    # the fixture would be testing an impossible shape rather than this branch.
    await db.record_signal_first_seen("coin-isoc", earlier.isoformat())
    await db._conn.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
        ("coin-isoc", "CISO", "Coin IsoC", now.isoformat()),
    )
    await db._conn.commit()

    comps = await compare_with_signals(db)
    assert len(comps) == 1
    assert comps[0].detected_by_chains is True
    assert comps[0].chains_lead_minutes == pytest.approx(120.0, abs=1.0)
    assert comps[0].is_gap is False


@pytest.mark.asyncio
async def test_compare_multiple_detections(db):
    """A token detected by both pipeline and chains."""
    now = datetime.now(timezone.utc)
    earlier_pipeline = now - timedelta(hours=4)
    earlier_chain = now - timedelta(hours=2)

    await db._conn.execute(
        """INSERT INTO candidates (contract_address, chain, token_name, ticker, first_seen_at)
           VALUES (?, ?, ?, ?, ?)""",
        ("coin-d", "solana", "Coin D", "CD", _sqlite_ts(earlier_pipeline)),
    )
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            "coin-d",
            "memecoin",
            "candidate_scored",
            "{}",
            "scorer",
            _sqlite_ts(earlier_chain),
        ),
    )
    # "CD" is a SHORT symbol -- see the note in test_compare_detects_chain_signal.
    await db.record_signal_first_seen("coin-d", _sqlite_ts(earlier_chain))
    await db._conn.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
        ("coin-d", "CD", "Coin D", _sqlite_ts(now)),
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
            (
                coin_id,
                coin_id.upper(),
                f"Coin {coin_id}",
                now.isoformat(),
                is_gap,
                narrative,
                lead,
                pipeline,
                pipeline_lead,
                0,
            ),
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
            (
                f"coin-{i}",
                f"C{i}",
                f"Coin {i}",
                (now - timedelta(hours=i)).isoformat(),
                1,
            ),
        )
    await db._conn.commit()

    results = await get_recent_comparisons(db, limit=10)
    assert len(results) == 2
    assert results[0]["coin_id"] == "coin-0"


# ---------------------------------------------------------------------------
# store_trending_from_candidates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_store_trending_from_candidates(db):
    """Stores snapshots from CandidateToken list without an API call."""
    candidates = [
        CandidateToken(
            contract_address="bless-network",
            chain="coingecko",
            token_name="Bless Network",
            ticker="BLESS",
            cg_trending_rank=1,
            holder_count=0,
            holder_growth_1h=0,
        ),
        CandidateToken(
            contract_address="pepe-coin",
            chain="coingecko",
            token_name="Pepe",
            ticker="PEPE",
            cg_trending_rank=2,
            holder_count=0,
            holder_growth_1h=0,
        ),
    ]

    snapshots = await store_trending_from_candidates(db, candidates)
    assert len(snapshots) == 2
    assert snapshots[0].coin_id == "bless-network"
    assert snapshots[0].symbol == "BLESS"
    assert snapshots[0].trending_score == 1.0
    assert snapshots[1].trending_score == 2.0

    cursor = await db._conn.execute("SELECT COUNT(*) FROM trending_snapshots")
    count = (await cursor.fetchone())[0]
    assert count == 2


@pytest.mark.asyncio
async def test_store_trending_from_candidates_empty(db):
    """Empty candidate list stores nothing."""
    snapshots = await store_trending_from_candidates(db, [])
    assert snapshots == []


# ---------------------------------------------------------------------------
# _parse_dt timezone handling
# ---------------------------------------------------------------------------


def test_parse_dt_naive_gets_utc():
    """Naive datetime string gets UTC timezone."""
    dt = _parse_dt("2024-01-15T10:30:00")
    assert dt.tzinfo is not None
    assert dt.tzinfo == timezone.utc


def test_parse_dt_aware_preserved():
    """Aware datetime string keeps its timezone."""
    dt = _parse_dt("2024-01-15T10:30:00+00:00")
    assert dt.tzinfo is not None


# ---------------------------------------------------------------------------
# compare_with_signals: LIKE prefix matching for signal_events
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_signal_events_like_matching(db):
    """Signal event with symbol 'bless' matches trending coin 'bless-network'."""
    now = datetime.now(timezone.utc)
    earlier = now - timedelta(hours=1)

    # Signal event stored with symbol (short form)
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("bless", "memecoin", "candidate_scored", "{}", "scorer", _sqlite_ts(earlier)),
    )
    # Mirror emit_event's atomic unit -- see the note in the isoformat test.
    # The LIKE matching under test is unchanged: signal_first_seen.token_id
    # holds the same values signal_events.token_id does.
    await db.record_signal_first_seen("bless", _sqlite_ts(earlier))

    # Trending snapshot uses CoinGecko slug (long form)
    await db._conn.execute(
        "INSERT INTO trending_snapshots (coin_id, symbol, name, snapshot_at) VALUES (?, ?, ?, ?)",
        ("bless-network", "BLESS", "Bless Network", _sqlite_ts(now)),
    )
    await db._conn.commit()

    comparisons = await compare_with_signals(db)
    assert len(comparisons) == 1
    comp = comparisons[0]
    assert comp.detected_by_chains is True
    assert comp.chains_lead_minutes > 0
    assert comp.is_gap is False


# ---------------------------------------------------------------------------
# Peak-update attribution. The two trackers' peak updaters are driven by the
# same two loops; `tracker=` lets a journald filter select one of them without
# pattern-matching the event name, and the noop event stops a run that updated
# nothing from being byte-identical (an empty log) to a run that never happened.
# ---------------------------------------------------------------------------


async def _seed_trending_peak_row(db, coin_id: str, *, peak: float, price: float):
    await db._conn.execute(
        """INSERT INTO trending_comparisons
           (coin_id, symbol, name, appeared_on_trending_at, detected_price,
            peak_price, is_gap)
           VALUES (?, ?, ?, datetime('now'), 1.0, ?, 1)""",
        (coin_id, coin_id[:3].upper(), coin_id.title(), peak),
    )
    await db._conn.execute(
        "INSERT INTO price_cache (coin_id, current_price, updated_at) "
        "VALUES (?, ?, datetime('now'))",
        (coin_id, price),
    )
    await db._conn.commit()


async def test_trending_peaks_updated_carries_the_tracker_label(db):
    """The sibling event was entirely unlabelled — nothing in the payload said
    which tracker produced it."""
    import structlog

    from scout.trending.tracker import update_trending_peaks

    await _seed_trending_peak_row(db, "peak-trend", peak=1.0, price=2.0)

    with structlog.testing.capture_logs() as log_events:
        assert await update_trending_peaks(db) == 1

    emitted = [e for e in log_events if e["event"] == "trending_tracker.peaks_updated"]
    assert len(emitted) == 1
    assert emitted[0]["tracker"] == "trending"
    assert emitted[0]["count"] == 1, "pre-existing count field must survive"


async def test_trending_peaks_noop_is_emitted_at_debug(db):
    """Rows joined, none higher than their stored peak: still says so."""
    import structlog

    from scout.trending.tracker import update_trending_peaks

    await _seed_trending_peak_row(db, "noop-trend", peak=9.0, price=2.0)

    with structlog.testing.capture_logs() as log_events:
        assert await update_trending_peaks(db) == 0

    noop = [e for e in log_events if e["event"] == "trending_tracker.peaks_noop"]
    assert len(noop) == 1
    assert noop[0]["log_level"] == "debug", "must not add info-level volume"
    assert noop[0]["tracker"] == "trending"
    assert noop[0]["count"] == 0
    assert noop[0]["examined"] == 1
    assert not [
        e for e in log_events if e["event"] == "trending_tracker.peaks_updated"
    ], "the updated event must not fire on a no-op run"


async def test_trending_and_gainers_peak_events_are_distinguishable(db):
    """The point of the label: one filter key separates the two trackers even
    though both loops call both updaters back to back."""
    import structlog

    from scout.gainers.tracker import update_gainers_peaks
    from scout.trending.tracker import update_trending_peaks

    with structlog.testing.capture_logs() as log_events:
        await update_trending_peaks(db)
        await update_gainers_peaks(db, caller="pipeline_cycle")

    trackers = [e["tracker"] for e in log_events if "tracker" in e]
    assert sorted(trackers) == ["gainers", "trending"], trackers
