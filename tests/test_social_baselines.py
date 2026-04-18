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
        interactions_ring=(),
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


def test_update_state_nan_skips_entirely():
    """NaN is treated like None — does NOT poison the EWMA."""
    state = _state(avg_social_volume_24h=100.0, sample_count=288)
    result = update_state(
        state, float("nan"), min_samples=288, spike_ratio=2.0
    )
    assert result.avg_social_volume_24h == 100.0
    assert result.sample_count == 288  # no increment per §5.4 null rule


def test_update_state_negative_value_skips_entirely():
    """Negative values do not update avg or sample_count."""
    state = _state(avg_social_volume_24h=100.0, sample_count=288)
    result = update_state(state, -50.0, min_samples=288, spike_ratio=2.0)
    assert result.avg_social_volume_24h == 100.0
    assert result.sample_count == 288


def test_update_state_first_sample_bootstraps_avg_directly():
    """A fresh baseline (sample_count=0, avg=0) seeds avg from the first
    real value -- EWMA-averaging against zero would poison the baseline at
    ``new_value / min_samples`` and make the coin look perpetually spiking.
    """
    state = _state(avg_social_volume_24h=0.0, sample_count=0)
    new = update_state(state, new_value=500.0, min_samples=288, spike_ratio=2.0)
    assert new.avg_social_volume_24h == 500.0
    assert new.sample_count == 1


def test_update_state_during_warmup_always_ewma():
    """Before warmup, spike-exclusion does not apply -- everything goes into avg."""
    state = _state(avg_social_volume_24h=100.0, sample_count=10)
    # A 10x value DURING warmup should still update (no exclusion rule yet).
    new = update_state(state, new_value=1000.0, min_samples=288, spike_ratio=2.0)
    # Not frozen: avg actually moved.
    assert new.avg_social_volume_24h != 100.0
    assert new.sample_count == 11


@pytest.mark.asyncio
async def test_hydrate_handles_malformed_ring_json(db):
    """A row with unparseable interactions_ring JSON hydrates with ring=()
    rather than crashing the whole social loop.
    """
    # Directly insert a row with an intentionally broken ring.
    await db._conn.execute(
        """INSERT INTO social_baselines (
            coin_id, symbol, avg_social_volume_24h, avg_galaxy_score,
            last_galaxy_score, interactions_ring, sample_count,
            last_poll_at, last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "foo", "FOO", 100.0, 50.0, 50.0,
            "{not-json",  # unparseable
            42,
            None,
            "2026-04-18T00:00:00+00:00",
        ),
    )
    await db._conn.commit()

    cache = BaselineCache()
    loaded = await hydrate_baselines(db, cache)
    assert loaded == 1
    state = cache.get("foo")
    assert state is not None
    assert state.interactions_ring == ()  # safe default on corrupt JSON
    assert state.sample_count == 42  # non-ring fields still hydrated


@pytest.mark.asyncio
async def test_hydrate_handles_ring_wrong_type(db):
    """interactions_ring persisted as a JSON object (not a list) -> ring=()."""
    await db._conn.execute(
        """INSERT INTO social_baselines (
            coin_id, symbol, avg_social_volume_24h, avg_galaxy_score,
            last_galaxy_score, interactions_ring, sample_count,
            last_poll_at, last_updated
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            "foo", "FOO", 100.0, 50.0, 50.0,
            json.dumps({"not": "a list"}),
            42,
            None,
            "2026-04-18T00:00:00+00:00",
        ),
    )
    await db._conn.commit()

    cache = BaselineCache()
    await hydrate_baselines(db, cache)
    state = cache.get("foo")
    assert state is not None
    assert state.interactions_ring == ()


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

    ring: tuple[float, ...] = ()
    for v in [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]:
        ring = push_interactions(ring, v)
    assert ring == (1.0, 2.0, 3.0, 4.0, 5.0, 6.0)
    ring = push_interactions(ring, 7.0)
    assert len(ring) == 6
    assert ring == (2.0, 3.0, 4.0, 5.0, 6.0, 7.0)


def test_interactions_ring_is_immutable():
    """interactions_ring is a tuple — .append raises AttributeError."""
    state = BaselineState(
        coin_id="foo",
        symbol="FOO",
        avg_social_volume_24h=1.0,
        avg_galaxy_score=50.0,
        last_galaxy_score=50.0,
        interactions_ring=(1.0, 2.0),
        sample_count=1,
        last_poll_at=None,
        last_updated=datetime(2026, 4, 18, tzinfo=timezone.utc),
    )
    with pytest.raises(AttributeError):
        state.interactions_ring.append(3.0)  # type: ignore[attr-defined]


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
async def test_flush_rolls_back_and_remarks_dirty_on_mid_batch_failure(
    db, monkeypatch
):
    """If an INSERT raises partway through, the whole batch is rolled back
    and EVERY originally-dirty coin is re-marked so the next flush retries."""
    cache = BaselineCache()
    for cid in ("a", "b", "c"):
        cache.set(cid, _state(coin_id=cid, symbol=cid.upper()))
        cache.mark_dirty(cid)

    original_execute = db._conn.execute
    call_count = [0]

    async def _maybe_fail(*args, **kwargs):
        sql = args[0] if args else ""
        if isinstance(sql, str) and "INSERT OR REPLACE INTO social_baselines" in sql:
            call_count[0] += 1
            if call_count[0] == 2:
                raise RuntimeError("simulated insert failure")
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(db._conn, "execute", _maybe_fail)
    with pytest.raises(RuntimeError):
        await flush_baselines(db, cache)

    # After rollback + re-mark, the dirty set must again hold all 3 coins.
    assert cache.pop_dirty() == {"a", "b", "c"}

    # The rollback means no social_baselines rows actually persisted.
    cursor = await db._conn.execute("SELECT COUNT(*) FROM social_baselines")
    row = await cursor.fetchone()
    assert row[0] == 0


@pytest.mark.asyncio
async def test_flush_on_cancelled_error(tmp_path, monkeypatch):
    """Driving the REAL run_social_loop: a cancellation propagates out and
    the ``finally`` clause still flushes dirty baselines to the DB.

    Replaces the earlier fake-worker tautology (test gap #6 / fix #21).
    """
    from scout.config import Settings
    from scout.db import Database
    from scout.social.lunarcrush import loop as loop_mod

    s = Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        LUNARCRUSH_ENABLED=True,
        LUNARCRUSH_API_KEY="lc_key",
        LUNARCRUSH_POLL_INTERVAL=60,
        LUNARCRUSH_CHECKPOINT_EVERY_N_POLLS=0,  # no checkpoint during test
    )
    d = Database(tmp_path / "cancel.db")
    await d.initialize()

    try:
        # Pre-populate the cache via hydrate so a dirty entry exists.
        cache_seed = BaselineCache()
        cache_seed.set(
            "foo",
            _state(coin_id="foo", sample_count=999, avg_social_volume_24h=250.0),
        )
        cache_seed.mark_dirty("foo")
        await flush_baselines(d, cache_seed)

        class _StubClient:
            disabled = False

            def __init__(self, *a, **k):
                pass

            async def fetch_coins_list(self):
                # Park forever so cancellation is the only way out.
                await asyncio.sleep(3600)
                return [], 0

            async def close(self):
                pass

            _session = None

        # Force detect_spikes to also mark something dirty so the finally
        # flush has work. Easiest: pre-seed dirty before the loop starts.
        monkeypatch.setattr(loop_mod, "LunarCrushClient", _StubClient)

        shutdown = asyncio.Event()
        # We reach into the loop's internal cache via a side-channel: seed
        # the DB, then the loop's hydrate will load it; we pre-populate
        # dirty through a monkeypatch of hydrate_baselines that marks it.
        real_hydrate = loop_mod.hydrate_baselines

        async def _hydrate_and_dirty(db, cache):
            n = await real_hydrate(db, cache)
            # Mark foo dirty right after hydrate so the finally-flush has work.
            state = cache.get("foo")
            if state is not None:
                cache.set("foo", state._replace(sample_count=1234))
                cache.mark_dirty("foo")
            return n

        monkeypatch.setattr(loop_mod, "hydrate_baselines", _hydrate_and_dirty)

        task = asyncio.create_task(loop_mod.run_social_loop(s, d, shutdown))
        await asyncio.sleep(0.1)  # give loop a chance to enter fetch
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        cursor = await d._conn.execute(
            "SELECT sample_count FROM social_baselines WHERE coin_id = 'foo'"
        )
        row = await cursor.fetchone()
        assert row is not None
        # finally-flush committed the dirty post-state (1234), proving the
        # flush ran on the cancel path.
        assert row[0] == 1234
    finally:
        await d.close()
