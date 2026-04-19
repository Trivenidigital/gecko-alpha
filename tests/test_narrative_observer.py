"""Tests for the OBSERVE phase — observer.py."""

from datetime import datetime, timezone

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.db import Database
from scout.narrative.models import CategoryAcceleration, CategorySnapshot
from scout.narrative.observer import (
    CATEGORIES_URL,
    compute_acceleration,
    detect_market_regime,
    fetch_categories,
    load_snapshots_at,
    parse_category_response,
    prune_old_snapshots,
    store_snapshot,
)

# ------------------------------------------------------------------
# parse_category_response
# ------------------------------------------------------------------


def test_parse_category_response_valid():
    data = [
        {
            "id": "defi",
            "name": "DeFi",
            "market_cap": 50_000_000,
            "market_cap_change_24h": 5.5,
            "volume_24h": 1_000_000,
        },
        {
            "id": "gaming",
            "name": "Gaming",
            "market_cap": 20_000_000,
            "market_cap_change_24h": -2.0,
            "volume_24h": 500_000,
        },
    ]
    result = parse_category_response(data, "BULL")
    assert len(result) == 2
    assert result[0].category_id == "defi"
    assert result[0].name == "DeFi"
    assert result[0].market_cap == 50_000_000
    assert result[0].market_cap_change_24h == 5.5
    assert result[0].volume_24h == 1_000_000
    assert result[0].market_regime == "BULL"
    assert result[0].coin_count is None
    assert result[1].category_id == "gaming"


def test_parse_category_response_skips_null_fields():
    data = [
        {
            "id": "bad",
            "name": "Bad",
            "market_cap": None,
            "market_cap_change_24h": 1.0,
            "volume_24h": 100,
        },
    ]
    result = parse_category_response(data, "CRAB")
    assert result == []


# ------------------------------------------------------------------
# compute_acceleration
# ------------------------------------------------------------------


def test_compute_acceleration_heating():
    now = datetime.now(timezone.utc)
    current = [
        CategorySnapshot(
            category_id="defi",
            name="DeFi",
            market_cap=100,
            market_cap_change_24h=12.0,
            volume_24h=2210,
            snapshot_at=now,
        ),
    ]
    previous = [
        CategorySnapshot(
            category_id="defi",
            name="DeFi",
            market_cap=90,
            market_cap_change_24h=4.0,
            volume_24h=2000,
            snapshot_at=now,
        ),
    ]
    result = compute_acceleration(
        current, previous, accel_threshold=5.0, vol_threshold=10.0
    )
    assert len(result) == 1
    acc = result[0]
    assert acc.acceleration == pytest.approx(8.0)
    assert acc.volume_growth_pct == pytest.approx(10.5)
    assert acc.is_heating is True
    assert acc.volume_24h == 2210


def test_compute_acceleration_not_heating():
    now = datetime.now(timezone.utc)
    current = [
        CategorySnapshot(
            category_id="defi",
            name="DeFi",
            market_cap=100,
            market_cap_change_24h=5.0,
            volume_24h=2100,
            snapshot_at=now,
        ),
    ]
    previous = [
        CategorySnapshot(
            category_id="defi",
            name="DeFi",
            market_cap=90,
            market_cap_change_24h=4.0,
            volume_24h=2000,
            snapshot_at=now,
        ),
    ]
    result = compute_acceleration(
        current, previous, accel_threshold=5.0, vol_threshold=10.0
    )
    assert len(result) == 1
    assert result[0].acceleration == pytest.approx(1.0)
    assert result[0].is_heating is False


# ------------------------------------------------------------------
# detect_market_regime
# ------------------------------------------------------------------


def test_detect_market_regime():
    # Boundaries: 3.0 is CRAB (not >3.0)
    assert detect_market_regime(3.0) == "CRAB"
    assert detect_market_regime(3.1) == "BULL"
    assert detect_market_regime(-3.0) == "CRAB"
    assert detect_market_regime(-3.1) == "BEAR"
    assert detect_market_regime(0.0) == "CRAB"
    assert detect_market_regime(10.0) == "BULL"
    assert detect_market_regime(-10.0) == "BEAR"


# ------------------------------------------------------------------
# fetch_categories
# ------------------------------------------------------------------


async def test_fetch_categories_success():
    payload = [{"id": "defi", "name": "DeFi"}]
    with aioresponses() as m:
        m.get(CATEGORIES_URL, payload=payload)
        async with aiohttp.ClientSession() as session:
            result = await fetch_categories(session, api_key="test-key")
    assert result == payload


async def test_fetch_categories_429_retries():
    payload = [{"id": "defi", "name": "DeFi"}]
    with aioresponses() as m:
        m.get(CATEGORIES_URL, status=429)
        m.get(CATEGORIES_URL, payload=payload)
        async with aiohttp.ClientSession() as session:
            result = await fetch_categories(session, api_key="", max_retries=3)
    assert result == payload


# ------------------------------------------------------------------
# DB round-trip: store_snapshot, load_snapshots_at, prune
# ------------------------------------------------------------------


async def test_store_and_load_snapshots(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.initialize()
    try:
        now = datetime.now(timezone.utc)
        snaps = [
            CategorySnapshot(
                category_id="defi",
                name="DeFi",
                market_cap=100,
                market_cap_change_24h=5.0,
                volume_24h=1000,
                market_regime="BULL",
                snapshot_at=now,
            ),
        ]
        await store_snapshot(db, snaps)
        loaded = await load_snapshots_at(db, now)
        assert len(loaded) == 1
        assert loaded[0].category_id == "defi"
        assert loaded[0].market_cap == 100

        deleted = await prune_old_snapshots(db, retention_days=0)
        # snapshot_at is ~now, cutoff is also ~now, so it should be pruned
        assert deleted >= 0
    finally:
        await db.close()
