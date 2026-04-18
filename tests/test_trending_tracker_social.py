"""Tests for the LunarCrush fourth-tier social detection in the trending tracker.

Mirrors the existing narrative/pipeline/chains tier tests but against the
``social_signals`` table. See design spec §11.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.trending.tracker import (
    _check_detector,
    compare_with_signals,
    get_recent_comparisons,
    get_trending_stats,
)


def _sqlite_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "social_trending.db")
    await d.initialize()
    yield d
    await d.close()


async def _insert_trending_snapshot(
    db: Database, coin_id: str, symbol: str, name: str, snapshot_at: datetime
) -> None:
    await db._conn.execute(
        """INSERT INTO trending_snapshots
           (coin_id, symbol, name, market_cap_rank, trending_score, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (coin_id, symbol, name, 100, 1.0, _sqlite_ts(snapshot_at)),
    )
    await db._conn.commit()


async def _insert_social_signal(
    db: Database, coin_id: str, symbol: str, detected_at: datetime
) -> None:
    await db._conn.execute(
        """INSERT INTO social_signals (
            coin_id, symbol, name,
            fired_social_volume_24h, fired_galaxy_jump, fired_interactions_accel,
            detected_at
        ) VALUES (?, ?, ?, 1, 0, 0, ?)""",
        (coin_id, symbol, symbol, _sqlite_ts(detected_at)),
    )
    await db._conn.commit()


# ---------------------------------------------------------------------------
# Detection + lead-minutes
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# _check_detector helper regression guard (design spec §12 refactor)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_detector_direct_social_tier_match(db):
    """Directly exercise _check_detector against social_signals to pin down
    the regression surface. If the helper ever stops honouring either
    ``coin_id`` or LOWER(symbol), this asserts first -- before the tier
    integration tests go red in a noisier way.
    """
    trending_at = datetime.now(timezone.utc) - timedelta(hours=1)
    social_at = trending_at - timedelta(minutes=30)
    await _insert_social_signal(db, "foo", "FOO", social_at)

    # Exact coin_id match path.
    detected, detected_at, lead = await _check_detector(
        db,
        table_name="social_signals",
        id_col="coin_id",
        coin_id="foo",
        symbol="DOES_NOT_MATCH",
        first_trending_at=_sqlite_ts(trending_at),
    )
    assert detected is True
    assert detected_at is not None
    assert 29.0 <= (lead or 0) <= 31.0

    # Case-insensitive symbol-fallback path.
    detected, detected_at, lead = await _check_detector(
        db,
        table_name="social_signals",
        id_col="coin_id",
        coin_id="UNMATCHED_ID",
        symbol="foo",  # stored row has symbol='FOO'
        first_trending_at=_sqlite_ts(trending_at),
    )
    assert detected is True
    assert lead is not None


@pytest.mark.asyncio
async def test_check_detector_lead_floors_at_zero_within_tolerance(db):
    """A detection AFTER trending but within the 5-minute tolerance window
    still returns detected=True with lead=0 (not a negative number).
    """
    trending_at = datetime.now(timezone.utc) - timedelta(hours=1)
    social_at = trending_at + timedelta(minutes=2)  # 2m AFTER trending
    await _insert_social_signal(db, "foo", "FOO", social_at)

    detected, _, lead = await _check_detector(
        db,
        table_name="social_signals",
        id_col="coin_id",
        coin_id="foo",
        symbol="FOO",
        first_trending_at=_sqlite_ts(trending_at),
    )
    assert detected is True
    assert lead == 0


@pytest.mark.asyncio
async def test_check_detector_no_match_returns_false_triple(db):
    """A coin with no row in the given table returns (False, None, None)."""
    trending_at = datetime.now(timezone.utc) - timedelta(hours=1)
    detected, detected_at, lead = await _check_detector(
        db,
        table_name="social_signals",
        id_col="coin_id",
        coin_id="never-seen",
        symbol="NOPE",
        first_trending_at=_sqlite_ts(trending_at),
    )
    assert detected is False
    assert detected_at is None
    assert lead is None


@pytest.mark.asyncio
async def test_compare_detects_coin_via_social_signals(db):
    """A coin spotted by LunarCrush before trending sets the social tier fields."""
    trending_at = datetime.now(timezone.utc) - timedelta(hours=1)
    social_at = trending_at - timedelta(minutes=45)
    await _insert_trending_snapshot(db, "foo", "FOO", "Foo Coin", trending_at)
    await _insert_social_signal(db, "foo", "FOO", social_at)

    comps = await compare_with_signals(db)
    assert len(comps) == 1
    comp = comps[0]
    assert comp.detected_by_social is True
    assert comp.social_detected_at is not None
    # Lead within a minute of 45m
    assert comp.social_lead_minutes is not None
    assert 44.0 <= comp.social_lead_minutes <= 46.0
    assert comp.is_gap is False


@pytest.mark.asyncio
async def test_social_detection_symbol_match_case_insensitive(db):
    """Fallback symbol match catches cases where coin_id differs but symbol matches."""
    trending_at = datetime.now(timezone.utc) - timedelta(hours=2)
    social_at = trending_at - timedelta(minutes=20)
    await _insert_trending_snapshot(db, "bar-token", "BAR", "Bar Token", trending_at)
    # Stored coin_id differs; lowercase symbol must still match.
    await _insert_social_signal(db, "bar", "bar", social_at)

    comps = await compare_with_signals(db)
    assert len(comps) == 1
    assert comps[0].detected_by_social is True


@pytest.mark.asyncio
async def test_social_detection_after_trending_not_marked(db):
    """A social signal that fires AFTER trending (beyond tolerance) must NOT be
    treated as a pre-trending detection."""
    trending_at = datetime.now(timezone.utc) - timedelta(hours=1)
    social_at = trending_at + timedelta(minutes=15)  # beyond 5m tolerance
    await _insert_trending_snapshot(db, "late", "LATE", "Late Coin", trending_at)
    await _insert_social_signal(db, "late", "LATE", social_at)

    comps = await compare_with_signals(db)
    assert len(comps) == 1
    assert comps[0].detected_by_social is False
    assert comps[0].social_lead_minutes is None
    assert comps[0].is_gap is True


# ---------------------------------------------------------------------------
# Persistence (INSERT + round-trip)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compare_persists_social_tier_columns(db):
    """Running compare twice round-trips the social-tier columns correctly."""
    trending_at = datetime.now(timezone.utc) - timedelta(hours=1)
    social_at = trending_at - timedelta(minutes=30)
    await _insert_trending_snapshot(db, "foo", "FOO", "Foo Coin", trending_at)
    await _insert_social_signal(db, "foo", "FOO", social_at)

    await compare_with_signals(db)
    rows = await get_recent_comparisons(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["detected_by_social"] == 1
    assert row["social_detected_at"] is not None
    assert row["social_lead_minutes"] is not None
    assert 29.0 <= row["social_lead_minutes"] <= 31.0


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_trending_stats_counts_social_tier(db):
    """by_social is populated and social lead times feed into avg/best_lead."""
    now = datetime.now(timezone.utc)
    trending_at = now - timedelta(hours=1)
    social_at = trending_at - timedelta(minutes=60)  # 60 minute lead
    await _insert_trending_snapshot(db, "foo", "FOO", "Foo Coin", trending_at)
    await _insert_social_signal(db, "foo", "FOO", social_at)
    await compare_with_signals(db)

    stats = await get_trending_stats(db)
    assert stats.by_social == 1
    assert stats.total_tracked == 1
    assert stats.caught_before_trending == 1
    # Social lead is the only lead; avg + best should reflect it.
    assert stats.avg_lead_minutes is not None
    assert stats.best_lead_minutes is not None
    assert 59.0 <= stats.avg_lead_minutes <= 61.0


@pytest.mark.asyncio
async def test_get_trending_stats_social_only_does_not_double_count(db):
    """A coin caught only by social should increment caught by 1, not by tier count."""
    now = datetime.now(timezone.utc)
    trending_at = now - timedelta(hours=1)
    await _insert_trending_snapshot(db, "a", "AAA", "Alpha", trending_at)
    await _insert_trending_snapshot(db, "b", "BBB", "Beta", trending_at)
    await _insert_social_signal(db, "a", "AAA", trending_at - timedelta(minutes=10))
    # b is never caught
    await compare_with_signals(db)

    stats = await get_trending_stats(db)
    assert stats.total_tracked == 2
    assert stats.caught_before_trending == 1
    assert stats.missed == 1
    assert stats.by_social == 1
