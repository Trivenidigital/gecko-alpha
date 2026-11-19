"""signal_events pruning must not be gated on active chain patterns.

``signal_events`` is the largest table in the production database (2.04 GB /
6.9M rows / ~550K rows per day against a 14-day retention). Its prune used to
live in ``_prune_stale`` inside the chains engine, reachable only *after*
``check_chains`` had passed its early return on an empty
``load_active_patterns()`` result. Production runs 3 active patterns today, so
it fired — but at zero active patterns the largest table in the database would
stop pruning silently, with no watchdog. Housekeeping must not sit beneath a
"there is useful chain work to do" condition.

These tests pin the relocation to ``_run_hourly_maintenance``, which runs every
hour regardless of chain state, and pin that exactly one call path survives.
Retention is unchanged at 14 days — this is a WHEN/WHETHER fix, not a
how-much fix.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from scout.chains.patterns import load_active_patterns, seed_built_in_patterns
from scout.chains.tracker import check_chains
from scout.db import Database
from scout.main import _run_hourly_maintenance

_PRUNE_DEFAULTS = dict(
    CHAIN_CHECK_INTERVAL_SEC=300,
    CHAIN_MAX_WINDOW_HOURS=24.0,
    CHAIN_EVENT_RETENTION_DAYS=14,
    CHAIN_ACTIVE_RETENTION_DAYS=7,
    CHAINS_ENABLED=True,
    # Out of scope for a prune test, and each would do real work against the
    # tmp_path DB. Narrow the hourly pass to the prune table under test.
    SQLITE_WAL_CHECKPOINT_ENABLED=False,
    SQLITE_INCREMENTAL_VACUUM_ENABLED=False,
    SQLITE_STALE_READER_WATCHDOG_ENABLED=False,
    SQLITE_WAL_PROFILE_ENABLED=False,
    CONVICTION_PROSPECTIVE_ENABLED=False,
)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def settings(settings_factory, tmp_path):
    return settings_factory(DB_PATH=tmp_path / "test.db", **_PRUNE_DEFAULTS)


async def _insert_event_aged(db: Database, token_id: str, age_days: float) -> None:
    """Insert one signal_event whose created_at is ``age_days`` in the past."""
    when = datetime.now(timezone.utc) - timedelta(days=age_days)
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            token_id,
            "memecoin",
            "candidate_scored",
            json.dumps({}),
            "scorer",
            when.isoformat(),
        ),
    )
    await db._conn.commit()


async def _event_tokens(db: Database) -> set[str]:
    async with db._conn.execute("SELECT token_id FROM signal_events") as cur:
        rows = await cur.fetchall()
    return {r["token_id"] for r in rows}


async def _hourly(db, settings) -> None:
    await _run_hourly_maintenance(db, MagicMock(), settings, MagicMock())


async def test_signal_events_pruned_with_zero_active_patterns(db, settings):
    """THE discriminating case: zero active patterns, stale rows still pruned.

    A fresh DB has no seeded patterns, so ``check_chains`` takes its
    ``chain_no_active_patterns`` early return and the chains engine never
    reaches its prune. The hourly maintenance pass must prune anyway.
    """
    assert len(await load_active_patterns(db)) == 0

    await _insert_event_aged(db, "0xstale", age_days=30)
    await _insert_event_aged(db, "0xfresh", age_days=1)

    # The chains engine cannot prune here — it returns before doing any work.
    await check_chains(db, settings)
    assert await _event_tokens(db) == {"0xstale", "0xfresh"}

    await _hourly(db, settings)

    assert await _event_tokens(db) == {"0xfresh"}


async def test_chains_engine_no_longer_prunes_signal_events(db, settings):
    """With patterns active, the chains engine must NOT prune signal_events.

    Leaving the old call site live alongside the new one would be harmless at
    runtime (a second DELETE finds nothing) but is a maintenance trap: two
    owners for one table's retention. Ownership is now the hourly pass alone.
    """
    await seed_built_in_patterns(db)
    assert len(await load_active_patterns(db)) > 0

    await _insert_event_aged(db, "0xstale", age_days=30)

    await check_chains(db, settings)

    assert await _event_tokens(db) == {"0xstale"}


async def test_signal_events_pruned_exactly_once_per_hourly_pass(
    db, settings, monkeypatch
):
    """One call path, one call: a full chains-pass + hourly-pass prunes once."""
    await seed_built_in_patterns(db)
    calls: list[dict] = []
    real = db.prune_signal_events

    async def _spy(**kwargs):
        calls.append(kwargs)
        return await real(**kwargs)

    monkeypatch.setattr(db, "prune_signal_events", _spy)

    await check_chains(db, settings)
    await _hourly(db, settings)

    assert len(calls) == 1
    assert calls[0] == {"keep_days": settings.CHAIN_EVENT_RETENTION_DAYS}


async def test_retention_boundary_unchanged_at_fourteen_days(db, settings):
    """Retention is 14 days, sourced from Settings. Nothing older survives,
    nothing newer is deleted."""
    assert settings.CHAIN_EVENT_RETENTION_DAYS == 14

    await _insert_event_aged(db, "0xday15", age_days=15)
    await _insert_event_aged(db, "0xday14_1", age_days=14.1)
    await _insert_event_aged(db, "0xday13_9", age_days=13.9)
    await _insert_event_aged(db, "0xday1", age_days=1)

    await _hourly(db, settings)

    assert await _event_tokens(db) == {"0xday13_9", "0xday1"}


async def test_prune_signal_events_honours_non_default_retention(db, settings_factory):
    """keep_days is read from Settings, not hardcoded."""
    settings = settings_factory(
        DB_PATH=db._db_path,
        **{
            **_PRUNE_DEFAULTS,
            "CHAIN_EVENT_RETENTION_DAYS": 3,
            # Ruling F residual: signal_events retention must cover the
            # prospective watchlist's lookback, which still derives first-seen
            # from that table. Lowering retention alone is now a config error.
            "CONVICTION_PROSPECTIVE_LOOKBACK_DAYS": 3,
        },
    )

    await _insert_event_aged(db, "0xday5", age_days=5)
    await _insert_event_aged(db, "0xday2", age_days=2)

    await _hourly(db, settings)

    assert await _event_tokens(db) == {"0xday2"}


# ---------------------------------------------------------------------------
# §12a prune-freshness observability
# ---------------------------------------------------------------------------
# Relocating the prune fixes WHERE it runs. On its own it does not make a
# future silent stop DETECTABLE: under the previous `if rows:` guard, "the
# prune ran and found nothing" and "the prune never ran" wrote byte-identical
# journals. These tests pin the discriminating pair.


def _pruned_events(logger, event_base: str) -> list[dict]:
    """Every ``{event_base}_pruned`` kwargs dict captured on a mock logger."""
    return [
        call.kwargs
        for call in logger.info.call_args_list
        if call.args and call.args[0] == f"{event_base}_pruned"
    ]


async def test_prune_emits_freshness_event_even_when_nothing_deleted(db, settings):
    """THE discriminating case for observability: ran, deleted 0, still logged.

    Nothing in this DB is old enough to prune. A run that emits no event here
    is indistinguishable from a run that never happened — which is the whole
    failure mode this repair exists to close.
    """
    await _insert_event_aged(db, "0xfresh", age_days=1)
    logger = MagicMock()

    await _run_hourly_maintenance(db, MagicMock(), settings, logger)

    events = _pruned_events(logger, "signal_events")
    # Existence AND count, never `if events:` — a guarded assertion passes
    # vacuously at exactly the moment the observable disappears.
    assert len(events) == 1
    assert events[0]["rows_deleted"] == 0
    assert events[0]["keep_days"] == settings.CHAIN_EVENT_RETENTION_DAYS

    # And the row really was untouched — the 0 is honest, not a swallowed error.
    assert await _event_tokens(db) == {"0xfresh"}


async def test_prune_freshness_event_reports_real_rowcount(db, settings):
    """Same event, non-zero count — so the two cases are told apart by VALUE."""
    await _insert_event_aged(db, "0xstale1", age_days=30)
    await _insert_event_aged(db, "0xstale2", age_days=20)
    await _insert_event_aged(db, "0xfresh", age_days=1)
    logger = MagicMock()

    await _run_hourly_maintenance(db, MagicMock(), settings, logger)

    events = _pruned_events(logger, "signal_events")
    assert len(events) == 1
    assert events[0]["rows_deleted"] == 2
    assert await _event_tokens(db) == {"0xfresh"}


async def test_every_prune_in_the_hourly_loop_reports_freshness(db, settings):
    """The guard was shared, so the blind spot was shared. All 8 report.

    signal_events was the table that made this urgent, but it sat in a loop
    with seven siblings under the same `if rows:` guard. Fixing only the one
    would leave the identical ambiguity in place for the rest.
    """
    logger = MagicMock()

    await _run_hourly_maintenance(db, MagicMock(), settings, logger)

    for event_base in (
        "volume_spikes",
        "momentum_7d",
        "trending_snapshots",
        "learn_logs",
        "chain_matches",
        "holder_snapshots",
        "conviction_watchlist_snapshots",
        "signal_events",
    ):
        events = _pruned_events(logger, event_base)
        assert len(events) == 1, f"{event_base} emitted {len(events)} freshness events"
        assert events[0]["rows_deleted"] == 0
