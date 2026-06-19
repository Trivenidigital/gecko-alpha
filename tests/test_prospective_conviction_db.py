"""conviction_watchlist_snapshots table + DB methods (Task 2)."""

from scout.db import Database


async def _db(tmp_path):
    db = Database(str(tmp_path / "c.db"))
    await db.initialize()
    return db


def _row(coin_id, tier, early=4, fresh=1, mcap=12_000_000.0):
    return {
        "coin_id": coin_id,
        "symbol": coin_id.upper(),
        "name": coin_id.title(),
        "early_count": early,
        "fresh_count": fresh,
        "tier": tier,
        "contributing_surfaces": ["chains", "spikes", "momentum", "velocity"],
        "market_cap": mcap,
        "mcap_age_minutes": 30.0,
        "first_detection_ages": {"chains": 2000.0},
    }


async def test_insert_and_latest_roundtrip(tmp_path):
    db = await _db(tmp_path)
    assert await db.latest_conviction_watchlist_snapshot_at() is None
    n = await db.insert_conviction_watchlist_snapshot(
        [_row("pepe", "high")], "2026-06-19T00:00:00+00:00"
    )
    assert n == 1
    assert (
        await db.latest_conviction_watchlist_snapshot_at()
        == "2026-06-19T00:00:00+00:00"
    )
    latest = await db.get_latest_conviction_watchlist()
    assert latest[0]["coin_id"] == "pepe"
    assert latest[0]["tier"] == "high"
    assert latest[0]["contributing_surfaces"] == [
        "chains",
        "spikes",
        "momentum",
        "velocity",
    ]
    assert latest[0]["market_cap"] == 12_000_000.0
    assert latest[0]["first_detection_ages"] == {"chains": 2000.0}
    await db.close()


async def test_latest_returns_only_newest_batch(tmp_path):
    db = await _db(tmp_path)
    await db.insert_conviction_watchlist_snapshot(
        [_row("a", "watch", early=2, fresh=0, mcap=None)], "2026-06-19T00:00:00+00:00"
    )
    await db.insert_conviction_watchlist_snapshot(
        [_row("b", "high")], "2026-06-19T01:00:00+00:00"
    )
    latest = await db.get_latest_conviction_watchlist()
    assert [r["coin_id"] for r in latest] == ["b"]
    await db.close()


async def test_null_mcap_roundtrips(tmp_path):
    db = await _db(tmp_path)
    r = _row("nomcap", "watch", early=2, fresh=0, mcap=None)
    r["mcap_age_minutes"] = None
    await db.insert_conviction_watchlist_snapshot([r], "2026-06-19T00:00:00+00:00")
    latest = await db.get_latest_conviction_watchlist()
    assert latest[0]["market_cap"] is None
    assert latest[0]["mcap_age_minutes"] is None
    await db.close()


async def test_prune_by_retention(tmp_path):
    db = await _db(tmp_path)
    await db.insert_conviction_watchlist_snapshot(
        [_row("old", "watch")], "2026-01-01T00:00:00+00:00"
    )
    deleted = await db.prune_conviction_watchlist_snapshots(keep_days=30)
    assert deleted == 1
    assert await db.latest_conviction_watchlist_snapshot_at() is None
    await db.close()
