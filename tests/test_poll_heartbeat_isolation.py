"""Generic starvation counters must not manufacture successful poll heartbeats."""

import pytest

from scout import heartbeat
from scout.db import Database


@pytest.fixture(autouse=True)
def isolated_heartbeat():
    heartbeat._reset_heartbeat_stats()
    yield
    heartbeat._reset_heartbeat_stats()


@pytest.mark.parametrize("source", ["rh_pons", "dex_discovery"])
async def test_restart_does_not_refresh_stale_poll_heartbeat(
    tmp_path, settings_factory, source
):
    db = Database(tmp_path / "heartbeat.db")
    await db.initialize()
    try:
        await db.upsert_ingest_watchdog_state(source, 0)
        await db._conn.execute(
            "UPDATE ingest_watchdog_state SET updated_at=? WHERE source=?",
            ("2001-01-01 00:00:00", source),
        )
        await db._conn.commit()
        settings = settings_factory()
        await heartbeat.hydrate_ingest_watchdog_state(db, settings)
        # Even accidental source samples must not enroll successful-poll-only
        # rows into generic per-cycle persistence.
        heartbeat.observe_ingest_sources(
            [
                heartbeat.IngestSourceSample(source=source, raw_count=0),
                heartbeat.IngestSourceSample(source="coingecko", raw_count=0),
            ],
            settings,
        )
        await heartbeat.persist_ingest_watchdog_state(db)
        cursor = await db._conn.execute(
            "SELECT updated_at FROM ingest_watchdog_state WHERE source=?", (source,)
        )
        assert (await cursor.fetchone())[0] == "2001-01-01 00:00:00"
        assert source not in heartbeat._ingest_watchdog_state
        assert (await db.load_ingest_watchdog_state())["coingecko"] == 1
    finally:
        await db.close()


async def test_ordinary_starvation_survives_restart(tmp_path, settings_factory):
    db = Database(tmp_path / "heartbeat.db")
    await db.initialize()
    try:
        settings = settings_factory(INGEST_STARVATION_THRESHOLD_CYCLES=2)
        await db.upsert_ingest_watchdog_state("coingecko", 1)
        await heartbeat.hydrate_ingest_watchdog_state(db, settings)
        events = heartbeat.observe_ingest_sources(
            [heartbeat.IngestSourceSample(source="coingecko", raw_count=0)], settings
        )
        assert len(events) == 1
        assert events[0].kind == "starved"
        await heartbeat.persist_ingest_watchdog_state(db)
        assert (await db.load_ingest_watchdog_state())["coingecko"] == 2
    finally:
        await db.close()
