"""Tests for scout.social.baselines EWMA cache + DB checkpoint."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from scout.db import Database
from scout.social.baselines import (
    BaselineCache,
    flush_baselines,
    hydrate_baselines,
    update_state,
)
from scout.social.models import BaselineState


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "baselines.db")
    await d.initialize()
    yield d
    await d.close()


def _state(**overrides) -> BaselineState:
    defaults = dict(
        coin_id="foo",
        symbol="FOO",
        avg_social_volume_24h=100.0,
        avg_galaxy_score=50.0,
        last_galaxy_score=50.0,
        interactions_ring=[],
        sample_count=288,
        last_poll_at=None,
        last_updated=datetime(2026, 4, 18, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return BaselineState(**defaults)


def test_update_state_ewma_applies_after_warmup():
    """Normal samples (within spike/collapse bounds) apply EWMA."""
    state = _state(avg_social_volume_24h=100.0, sample_count=288)
    new = update_state(state, new_value=110.0, min_samples=288, spike_ratio=2.0)
    # alpha = 1/288; new_avg = 1/288*110 + 287/288*100 -> between 100 and 110
    assert new.avg_social_volume_24h > 100.0
    assert new.avg_social_volume_24h < 110.0
    assert new.sample_count == 289


def test_update_state_spike_upward_skips_avg_keeps_count():
    """Upward spike (>=2x) freezes the avg but increments sample_count."""
    state = _state(avg_social_volume_24h=100.0, sample_count=288)
    new = update_state(state, new_value=300.0, min_samples=288, spike_ratio=2.0)
    assert new.avg_social_volume_24h == 100.0
    assert new.sample_count == 289


def test_update_state_collapse_downward_skips_avg_keeps_count():
    """Downward collapse (<=0.5x when ratio=2.0) freezes avg, still counts sample."""
    state = _state(avg_social_volume_24h=100.0, sample_count=288)
    new = update_state(state, new_value=40.0, min_samples=288, spike_ratio=2.0)
    assert new.avg_social_volume_24h == 100.0
    assert new.sample_count == 289


def test_update_state_null_value_skips_entirely():
    """None / zero does not drag the avg or bump sample_count."""
    state = _state(sample_count=10)
    assert update_state(state, None, min_samples=288, spike_ratio=2.0).sample_count == 10
    assert update_state(state, 0.0, min_samples=288, spike_ratio=2.0).sample_count == 10


def test_update_state_during_warmup_always_ewma():
    """Before warmup, spike-exclusion does not apply -- everything goes into avg."""
    state = _state(avg_social_volume_24h=100.0, sample_count=10)
    # A 10x value DURING warmup should still update (no exclusion rule yet).
    new = update_state(state, new_value=1000.0, min_samples=288, spike_ratio=2.0)
    # Not frozen: avg actually moved.
    assert new.avg_social_volume_24h != 100.0
    assert new.sample_count == 11


@pytest.mark.asyncio
async def test_hydrate_and_flush_survives_restart(db, tmp_path):
    """Flushing, closing, re-opening a DB preserves baseline rows."""
    cache = BaselineCache()
    cache.set(
        "foo",
        _state(coin_id="foo", avg_social_volume_24h=250.0, sample_count=500),
    )
    # Mark dirty + flush
    cache.mark_dirty("foo")
    await flush_baselines(db, cache)

    await db.close()

    # Reopen and hydrate
    d2 = Database(tmp_path / "baselines.db")
    await d2.initialize()
    cache2 = BaselineCache()
    await hydrate_baselines(d2, cache2)
    await d2.close()

    state = cache2.get("foo")
    assert state is not None
    assert state.sample_count == 500
    assert state.avg_social_volume_24h == 250.0


def test_interactions_ring_rotates_on_overflow():
    """A 6-slot ring truncates the oldest value on the 7th write."""
    from scout.social.baselines import push_interactions

    ring: list[float] = []
    for v in [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]:
        ring = push_interactions(ring, v)
    assert ring == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    ring = push_interactions(ring, 7.0)
    assert len(ring) == 6
    assert ring == [2.0, 3.0, 4.0, 5.0, 6.0, 7.0]


@pytest.mark.asyncio
async def test_flush_baselines_serializes_interactions_ring(db):
    """interactions_ring is persisted as JSON text."""
    cache = BaselineCache()
    cache.set(
        "foo",
        _state(coin_id="foo", interactions_ring=[1.0, 2.0, 3.0]),
    )
    cache.mark_dirty("foo")
    await flush_baselines(db, cache)

    cursor = await db._conn.execute(
        "SELECT interactions_ring FROM social_baselines WHERE coin_id = 'foo'"
    )
    row = await cursor.fetchone()
    assert row is not None
    parsed = json.loads(row[0])
    assert parsed == [1.0, 2.0, 3.0]


@pytest.mark.asyncio
async def test_flush_on_cancelled_error(db):
    """A CancelledError in the surrounding loop triggers finally-flush.

    We simulate by running a task that writes, then gets cancelled; the
    finally block flushes to DB before letting the cancellation propagate.
    """
    cache = BaselineCache()
    cache.set("foo", _state(coin_id="foo", sample_count=999))
    cache.mark_dirty("foo")

    async def worker() -> None:
        try:
            await asyncio.sleep(3600)  # would be cancelled
        except asyncio.CancelledError:
            raise
        finally:
            await flush_baselines(db, cache)

    task = asyncio.create_task(worker())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cursor = await db._conn.execute(
        "SELECT sample_count FROM social_baselines WHERE coin_id = 'foo'"
    )
    row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 999
