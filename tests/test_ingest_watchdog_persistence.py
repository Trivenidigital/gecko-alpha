"""GA-19: ingest-starvation watchdog state persistence across restarts.

The per-source consecutive-miss counters were previously a module-level
in-memory dict cleared on every boot (heartbeat._reset_heartbeat_stats,
called from main()). gecko-pipeline restarts (deploys, crash-bounces with
Restart=always) reset the counter, so a persistently-dead ingestion source
never accumulated INGEST_STARVATION_THRESHOLD_CYCLES misses across restarts
and `ingest_source_starved` never fired.

These tests cover the durable substrate (ingest_watchdog_state table +
UPSERT/load) and the heartbeat hydrate/persist round-trip that makes the
counter restart-durable while preserving all existing watchdog semantics.
"""

from types import SimpleNamespace

import pytest

from scout.db import Database
from scout.heartbeat import (
    IngestSourceSample,
    _ingest_watchdog_state,
    _reset_heartbeat_stats,
    hydrate_ingest_watchdog_state,
    observe_ingest_sources,
    persist_ingest_watchdog_state,
)


def _settings(threshold: int = 5) -> SimpleNamespace:
    return SimpleNamespace(
        INGEST_WATCHDOG_ENABLED=True,
        INGEST_STARVATION_THRESHOLD_CYCLES=threshold,
    )


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "watchdog_state.db"))
    await database.initialize()
    yield database
    await database.close()


@pytest.fixture(autouse=True)
def _clean_heartbeat_state():
    _reset_heartbeat_stats()
    yield
    _reset_heartbeat_stats()


# ---------------------------------------------------------------------------
# Migration + DB layer
# ---------------------------------------------------------------------------


async def test_migration_creates_ingest_watchdog_state_table(db):
    cur = await db._conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='ingest_watchdog_state'"
    )
    row = await cur.fetchone()
    assert row is not None
    ddl = row["sql"]
    assert "source" in ddl
    assert "consecutive_misses" in ddl
    assert "updated_at" in ddl


async def test_migration_is_idempotent_across_reinitialize(tmp_path):
    path = str(tmp_path / "idempotent.db")
    db1 = Database(path)
    await db1.initialize()
    await db1.upsert_ingest_watchdog_state("coingecko:markets", 3)
    await db1.close()

    # Simulated restart: fresh Database object, same file. Migration must
    # not error and must not clobber existing rows.
    db2 = Database(path)
    await db2.initialize()
    state = await db2.load_ingest_watchdog_state()
    assert state == {"coingecko:markets": 3}
    await db2.close()


async def test_upsert_updates_existing_row_in_place(db):
    await db.upsert_ingest_watchdog_state("dexscreener:boosts", 1)
    await db.upsert_ingest_watchdog_state("dexscreener:boosts", 2)

    cur = await db._conn.execute(
        "SELECT source, consecutive_misses, updated_at " "FROM ingest_watchdog_state"
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["source"] == "dexscreener:boosts"
    assert rows[0]["consecutive_misses"] == 2
    assert rows[0]["updated_at"]  # non-empty ISO timestamp


async def test_load_returns_all_sources(db):
    await db.upsert_ingest_watchdog_state("coingecko:markets", 4)
    await db.upsert_ingest_watchdog_state("geckoterminal:solana", 0)

    state = await db.load_ingest_watchdog_state()
    assert state == {"coingecko:markets": 4, "geckoterminal:solana": 0}


async def test_load_empty_table_returns_empty_dict(db):
    assert await db.load_ingest_watchdog_state() == {}


# ---------------------------------------------------------------------------
# Heartbeat hydrate / persist round-trip
# ---------------------------------------------------------------------------


async def test_counter_survives_simulated_restart(db):
    """3 misses -> restart -> 2 misses fires starved at cumulative 5."""
    settings = _settings(threshold=5)
    miss = [IngestSourceSample(source="coingecko:markets", raw_count=0)]

    for _ in range(3):
        events = observe_ingest_sources(miss, settings)
        assert events == []
    await persist_ingest_watchdog_state(db)

    # Simulated restart: in-memory state wiped, then hydrated from DB.
    _reset_heartbeat_stats()
    assert _ingest_watchdog_state == {}
    await hydrate_ingest_watchdog_state(db, settings)
    assert _ingest_watchdog_state["coingecko:markets"]["consecutive_empty"] == 3

    events = observe_ingest_sources(miss, settings)
    assert events == []  # cumulative 4 < 5
    events = observe_ingest_sources(miss, settings)
    assert len(events) == 1
    assert events[0].kind == "starved"
    assert events[0].consecutive_empty_cycles == 5


async def test_without_hydration_counter_would_reset(db):
    """Documents the pre-fix failure mode: reset without hydrate loses misses."""
    settings = _settings(threshold=5)
    miss = [IngestSourceSample(source="coingecko:markets", raw_count=0)]

    for _ in range(4):
        observe_ingest_sources(miss, settings)
    _reset_heartbeat_stats()  # restart WITHOUT hydration

    events = observe_ingest_sources(miss, settings)
    assert events == []  # counter restarted from zero — never fires


async def test_recovery_resets_persisted_row_to_zero(db):
    settings = _settings(threshold=5)
    source = "geckoterminal:solana"

    for _ in range(2):
        observe_ingest_sources(
            [IngestSourceSample(source=source, raw_count=0)], settings
        )
    await persist_ingest_watchdog_state(db)
    assert (await db.load_ingest_watchdog_state())[source] == 2

    observe_ingest_sources([IngestSourceSample(source=source, raw_count=7)], settings)
    await persist_ingest_watchdog_state(db)
    assert (await db.load_ingest_watchdog_state())[source] == 0


async def test_hydrate_at_or_above_threshold_marks_alerted_no_realert(db):
    """A source already past threshold pre-restart must not re-alert every
    boot during one long starvation episode (alert-once-per-episode
    semantics preserved), but must still emit recovered on recovery."""
    settings = _settings(threshold=5)
    source = "coingecko:markets"
    await db.upsert_ingest_watchdog_state(source, 7)

    await hydrate_ingest_watchdog_state(db, settings)
    assert _ingest_watchdog_state[source]["alerted"] is True

    events = observe_ingest_sources(
        [IngestSourceSample(source=source, raw_count=0)], settings
    )
    assert events == []  # still starved, already alerted — no duplicate

    events = observe_ingest_sources(
        [IngestSourceSample(source=source, raw_count=3)], settings
    )
    assert len(events) == 1
    assert events[0].kind == "recovered"


async def test_hydrate_below_threshold_not_alerted(db):
    settings = _settings(threshold=5)
    await db.upsert_ingest_watchdog_state("dexscreener:boosts", 2)

    await hydrate_ingest_watchdog_state(db, settings)
    state = _ingest_watchdog_state["dexscreener:boosts"]
    assert state["consecutive_empty"] == 2
    assert state["alerted"] is False
    assert state["last_success_at"] is None


async def test_persist_writes_every_tracked_source(db):
    settings = _settings(threshold=5)
    observe_ingest_sources(
        [
            IngestSourceSample(source="coingecko:markets", raw_count=0),
            IngestSourceSample(source="geckoterminal:solana", raw_count=9),
        ],
        settings,
    )
    await persist_ingest_watchdog_state(db)

    state = await db.load_ingest_watchdog_state()
    assert state == {"coingecko:markets": 1, "geckoterminal:solana": 0}


async def test_hydrate_on_empty_table_is_noop(db):
    settings = _settings(threshold=5)
    await hydrate_ingest_watchdog_state(db, settings)
    assert _ingest_watchdog_state == {}
