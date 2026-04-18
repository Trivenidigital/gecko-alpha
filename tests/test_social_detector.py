"""Tests for scout.social.lunarcrush.detector -- spike checks + orchestrator."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.social.baselines import BaselineCache
from scout.social.lunarcrush.detector import (
    check_galaxy_jump,
    check_interactions_accel,
    check_social_volume_24h_spike,
    detect_spikes,
)
from scout.social.models import BaselineState, SpikeKind


def _settings(**overrides) -> Settings:
    defaults = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        LUNARCRUSH_ENABLED=True,
        LUNARCRUSH_API_KEY="lc_key",
        LUNARCRUSH_SOCIAL_SPIKE_RATIO=2.0,
        LUNARCRUSH_GALAXY_JUMP=10.0,
        LUNARCRUSH_INTERACTIONS_ACCEL=3.0,
        LUNARCRUSH_TOP_N=10,
        LUNARCRUSH_DEDUP_HOURS=4,
        LUNARCRUSH_BASELINE_MIN_HOURS=24,
        LUNARCRUSH_BASELINE_MIN_SAMPLES=288,
        LUNARCRUSH_POLL_INTERVAL=300,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _state(**overrides) -> BaselineState:
    now = datetime(2026, 4, 18, tzinfo=timezone.utc)
    defaults = dict(
        coin_id="foo",
        symbol="FOO",
        avg_social_volume_24h=100.0,
        avg_galaxy_score=50.0,
        last_galaxy_score=50.0,
        interactions_ring=[],
        sample_count=300,
        last_poll_at=now,
        last_updated=now,
    )
    defaults.update(overrides)
    return BaselineState(**defaults)


# ---------------------------------------------------------------------------
# Per-kind checks
# ---------------------------------------------------------------------------


def test_check_social_volume_24h_spike_fires():
    state = _state(avg_social_volume_24h=100.0)
    coin = {"social_volume_24h": 250.0}
    hit = check_social_volume_24h_spike(coin, state, ratio=2.0)
    assert hit is not None
    assert hit > 1.0  # returns the spike ratio


def test_check_social_volume_24h_spike_below_threshold():
    state = _state(avg_social_volume_24h=100.0)
    coin = {"social_volume_24h": 150.0}  # 1.5x < 2.0
    assert check_social_volume_24h_spike(coin, state, ratio=2.0) is None


def test_check_galaxy_jump_fires():
    state = _state(last_galaxy_score=50.0)
    coin = {"galaxy_score": 70.0}
    jump = check_galaxy_jump(coin, state, min_jump=10.0)
    assert jump == 20.0


def test_check_galaxy_jump_below_threshold():
    state = _state(last_galaxy_score=50.0)
    coin = {"galaxy_score": 55.0}
    assert check_galaxy_jump(coin, state, min_jump=10.0) is None


def test_check_interactions_accel_requires_full_ring():
    """With <6 ring slots the check silently skips (returns None)."""
    state = _state(interactions_ring=[1_000.0, 2_000.0])
    coin = {"interactions_24h": 30_000.0}
    assert check_interactions_accel(coin, state, ratio=3.0) is None


def test_check_interactions_accel_fires():
    """Full 6-slot ring with 3x oldest value fires."""
    state = _state(interactions_ring=[1_000.0, 1_100.0, 1_200.0, 1_300.0, 1_400.0, 1_500.0])
    coin = {"interactions_24h": 5_000.0}  # 5x oldest (1000)
    ratio = check_interactions_accel(coin, state, ratio=3.0)
    assert ratio is not None
    assert ratio == 5.0


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "detector.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.mark.asyncio
async def test_detect_spikes_multi_kind_single_alert(db):
    """A coin that fires multiple kinds produces ONE alert listing all kinds."""
    s = _settings()
    cache = BaselineCache()
    cache.set(
        "astro",
        _state(
            coin_id="astro",
            symbol="AST",
            avg_social_volume_24h=100.0,
            last_galaxy_score=50.0,
            interactions_ring=[1_000.0] * 6,
            sample_count=300,
        ),
    )
    coins = [
        {
            "id": "astro",
            "symbol": "AST",
            "name": "Asteroid",
            "social_volume_24h": 500.0,
            "galaxy_score": 72.0,
            "interactions_24h": 10_000.0,
        }
    ]
    alerts, _ = await detect_spikes(db, s, cache, coins)
    assert len(alerts) == 1
    kinds = alerts[0].spike_kinds
    assert SpikeKind.SOCIAL_VOLUME_24H in kinds
    assert SpikeKind.GALAXY_JUMP in kinds
    assert SpikeKind.INTERACTIONS_ACCEL in kinds


@pytest.mark.asyncio
async def test_detect_spikes_cold_start_suppression(db):
    """Boundary: 287 samples skip, 288 fires."""
    s = _settings(LUNARCRUSH_BASELINE_MIN_HOURS=24, LUNARCRUSH_POLL_INTERVAL=300)
    cache = BaselineCache()
    # 287 samples -> required = 24*3600/300 = 288 -> SKIP
    cache.set(
        "foo",
        _state(
            coin_id="foo",
            symbol="FOO",
            avg_social_volume_24h=100.0,
            sample_count=287,
        ),
    )
    coins = [{"id": "foo", "symbol": "FOO", "name": "Foo", "social_volume_24h": 500.0}]
    alerts, _ = await detect_spikes(db, s, cache, coins)
    assert len(alerts) == 0

    # Bump to 288 -> should fire
    cache.set(
        "foo",
        _state(coin_id="foo", symbol="FOO", avg_social_volume_24h=100.0, sample_count=288),
    )
    alerts, _ = await detect_spikes(db, s, cache, coins)
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_detect_spikes_dedup_boundary_exactly_4h(db):
    """Dedup cutoff is >= cutoff-hours (not >), so a row exactly at 4h suppresses."""
    s = _settings(LUNARCRUSH_DEDUP_HOURS=4)
    cache = BaselineCache()
    cache.set(
        "foo",
        _state(coin_id="foo", symbol="FOO", avg_social_volume_24h=100.0, sample_count=300),
    )

    # Insert a row 3h 59m ago -- should dedup.
    await db._conn.execute(
        """INSERT INTO social_signals (coin_id, symbol, name, detected_at)
           VALUES ('foo', 'FOO', 'Foo', datetime('now', '-3 hours', '-59 minutes'))"""
    )
    await db._conn.commit()
    coins = [{"id": "foo", "symbol": "FOO", "name": "Foo", "social_volume_24h": 500.0}]
    alerts, _ = await detect_spikes(db, s, cache, coins)
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_detect_spikes_top_n(db):
    """With many qualifying coins, only TOP_N are returned."""
    s = _settings(LUNARCRUSH_TOP_N=3)
    cache = BaselineCache()
    for i in range(10):
        cid = f"c{i}"
        cache.set(
            cid,
            _state(
                coin_id=cid,
                symbol=f"C{i}",
                avg_social_volume_24h=100.0,
                sample_count=300,
            ),
        )
    coins = [
        {
            "id": f"c{i}",
            "symbol": f"C{i}",
            "name": f"Coin {i}",
            "social_volume_24h": 200.0 + i * 100.0,
        }
        for i in range(10)
    ]
    alerts, _ = await detect_spikes(db, s, cache, coins)
    assert len(alerts) == 3
    # Sorted by ratio descending: the highest social_volume_24h should lead.
    assert alerts[0].coin_id == "c9"


@pytest.mark.asyncio
async def test_detect_spikes_increments_sample_count_for_all_coins(db):
    """Non-firing coins still increment sample_count (§5.3 progress invariant)."""
    s = _settings()
    cache = BaselineCache()
    cache.set(
        "foo",
        _state(coin_id="foo", symbol="FOO", avg_social_volume_24h=100.0, sample_count=50),
    )
    coins = [{"id": "foo", "symbol": "FOO", "name": "Foo", "social_volume_24h": 110.0}]
    await detect_spikes(db, s, cache, coins)
    state = cache.get("foo")
    assert state is not None
    assert state.sample_count == 51
