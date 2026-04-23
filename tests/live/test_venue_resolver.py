"""Tests for VenueResolver (spec §7). Single-flight is FIRST test per §11.5."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from freezegun import freeze_time

from scout.db import Database
from scout.live.resolver import VenueResolver, OverrideStore


async def test_single_flight_one_miss_one_binance_call(tmp_path):
    """Spec §7.1: N=10 concurrent resolve('WBTC') during cache miss must issue
    ONE Binance exchangeInfo call. Thundering-herd protection."""
    db = Database(tmp_path / "t.db"); await db.initialize()
    adapter = AsyncMock()
    adapter.resolve_pair_for_symbol = AsyncMock(return_value="WBTCUSDT")
    resolver = VenueResolver(
        binance_adapter=adapter,
        override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1),
        negative_ttl=timedelta(seconds=60),
        db=db,
    )
    results = await asyncio.gather(*[resolver.resolve("WBTC") for _ in range(10)])
    assert all(r is not None and r.pair == "WBTCUSDT" for r in results)
    assert adapter.resolve_pair_for_symbol.call_count == 1
    await db.close()


async def test_resolver_cache_hit_skips_binance(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    adapter = AsyncMock()
    adapter.resolve_pair_for_symbol = AsyncMock(return_value="WBTCUSDT")
    resolver = VenueResolver(
        binance_adapter=adapter, override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1), negative_ttl=timedelta(seconds=60),
        db=db,
    )
    await resolver.resolve("WBTC")
    await resolver.resolve("WBTC")
    assert adapter.resolve_pair_for_symbol.call_count == 1
    await db.close()


async def test_override_row_overrides_exchange_info(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    await db._conn.execute(
        "INSERT INTO venue_overrides (symbol, venue, pair, note, disabled, "
        "created_at, updated_at) "
        "VALUES ('WBTC','binance','WBTCUSDT','manual','0','2026-04-23T00Z','2026-04-23T00Z')"
    )
    await db._conn.commit()
    adapter = AsyncMock()
    adapter.resolve_pair_for_symbol = AsyncMock(return_value="ZZZUSDT")
    resolver = VenueResolver(
        binance_adapter=adapter, override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1), negative_ttl=timedelta(seconds=60),
        db=db,
    )
    r = await resolver.resolve("WBTC")
    assert r.pair == "WBTCUSDT" and r.source == "override_table"
    assert adapter.resolve_pair_for_symbol.call_count == 0
    await db.close()


async def test_override_disabled_returns_none_with_disabled_flag(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    await db._conn.execute(
        "INSERT INTO venue_overrides (symbol, venue, pair, disabled, "
        "created_at, updated_at) "
        "VALUES ('WBTC','binance','WBTCUSDT',1,'2026-04-23T00Z','2026-04-23T00Z')"
    )
    await db._conn.commit()
    resolver = VenueResolver(
        binance_adapter=AsyncMock(), override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1), negative_ttl=timedelta(seconds=60),
        db=db,
    )
    r = await resolver.resolve("WBTC")
    # Disabled override → resolver returns None AND does NOT fall through to exchangeInfo
    assert r is None
    await db.close()


async def test_negative_cache_ttl_expires_at_60s(tmp_path):
    db = Database(tmp_path / "t.db"); await db.initialize()
    adapter = AsyncMock()
    adapter.resolve_pair_for_symbol = AsyncMock(return_value=None)
    resolver = VenueResolver(
        binance_adapter=adapter, override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1), negative_ttl=timedelta(seconds=60),
        db=db,
    )
    with freeze_time("2026-04-23 00:00:00") as frozen:
        assert await resolver.resolve("UNKNOWN") is None
        assert adapter.resolve_pair_for_symbol.call_count == 1

        frozen.move_to("2026-04-23 00:01:01")  # +61s
        assert await resolver.resolve("UNKNOWN") is None
        assert adapter.resolve_pair_for_symbol.call_count == 2
    await db.close()


async def test_locks_dict_is_bounded_after_resolutions(tmp_path):
    """Reviewer 2: the per-symbol _locks dict must not grow unboundedly.
    After N symbols resolve and no task is waiting on them, _locks should
    be empty (locks get evicted in the finally block once lock.locked()
    is False)."""
    db = Database(tmp_path / "t.db"); await db.initialize()
    adapter = AsyncMock()
    adapter.resolve_pair_for_symbol = AsyncMock(
        side_effect=lambda sym: f"{sym}USDT"
    )
    resolver = VenueResolver(
        binance_adapter=adapter, override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1), negative_ttl=timedelta(seconds=60),
        db=db,
    )
    # Sequential resolutions — each frees its lock on exit.
    for sym in ["AAA", "BBB", "CCC", "DDD", "EEE"]:
        await resolver.resolve(sym)
    assert resolver._locks == {}, (
        f"_locks leaked after sequential resolves: {list(resolver._locks)}"
    )

    # Concurrent single-flight on one symbol: all waiters share one Lock,
    # and after the last one exits the lock is evicted.
    await asyncio.gather(*[resolver.resolve("FFF") for _ in range(10)])
    assert "FFF" not in resolver._locks
    assert resolver._locks == {}
    await db.close()


async def test_resolver_increments_cache_hit_and_miss_metrics(tmp_path):
    """Spec §10.2: resolver reports resolver_cache_hits / resolver_cache_misses
    so operators can see whether the cache is actually saving Binance calls."""
    db = Database(tmp_path / "t.db"); await db.initialize()
    adapter = AsyncMock()
    adapter.resolve_pair_for_symbol = AsyncMock(return_value="WBTCUSDT")
    resolver = VenueResolver(
        binance_adapter=adapter, override_store=OverrideStore(db),
        positive_ttl=timedelta(hours=1), negative_ttl=timedelta(seconds=60),
        db=db,
    )
    await resolver.resolve("WBTC")   # miss → Binance call + positive cache
    await resolver.resolve("WBTC")   # hit  → no Binance call

    async def _val(metric):
        cur = await db._conn.execute(
            "SELECT value FROM live_metrics_daily WHERE metric = ?", (metric,)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    assert await _val("resolver_cache_misses") == 1
    assert await _val("resolver_cache_hits") == 1
    assert adapter.resolve_pair_for_symbol.call_count == 1
    await db.close()
