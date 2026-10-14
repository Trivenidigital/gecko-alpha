import pytest
from pydantic import ValidationError

from gecko.prune import prune
from gecko.settings import GeckoSettings

NOW = "2026-10-14T00:00:00Z"
OLD = "2026-03-01T00:00:00Z"  # > 180 d
MID = "2026-06-01T00:00:00Z"  # > 90 d, < 180 d


async def _count(store, sql):
    return (await (await store.conn.execute(sql)).fetchone())[0]


async def test_prunes_old_observations_but_not_recent(store, settings):
    await store.add_observation("base:x", "dexscreener", OLD, {"price_usd": 1})
    await store.add_observation("base:x", "dexscreener", MID, {"price_usd": 2})
    assert await prune(store, settings, NOW, batch=1) == (1, 0)
    assert await _count(store, "SELECT COUNT(*) FROM observations") == 1


async def test_benchmark_rows_survive_prune(store, settings):
    await store.add_observation("benchmark:btc", "coingecko", OLD, {"price_usd": 1})
    assert (await prune(store, settings, NOW))[0] == 0


async def test_runs_keep_earliest_ok_per_job(store, settings):
    await store.record_run("rank", OLD, OLD, False)
    await store.record_run("rank", OLD, OLD, True)  # earliest ok: kept
    await store.record_run("rank", MID, MID, True)  # old: deleted
    await store.record_run("ledger", MID, MID, True)  # earliest ok of ledger: kept
    await store.record_run("rank", NOW, NOW, True)
    assert (await prune(store, settings, NOW))[1] == 2
    rows = await (
        await store.conn.execute("SELECT job, finished_at, ok FROM runs ORDER BY id")
    ).fetchall()
    assert rows == [("rank", OLD, 1), ("ledger", MID, 1), ("rank", NOW, 1)]


def test_retention_floor_validated():
    with pytest.raises(ValidationError):
        GeckoSettings(obs_retention_days=29, _env_file=None)
    with pytest.raises(ValidationError):
        GeckoSettings(runs_retention_days=10, _env_file=None)
