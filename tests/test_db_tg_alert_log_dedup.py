"""BL-NEW-TG-ALERT-NOISE-DEDUP: tg_alert_log.outcome CHECK widening tests.

The dedup slice adds ONE new outcome value `blocked_dedup_24h` via the
table-rebuild migration `_migrate_tg_alert_log_dedup_outcome`, preserving ALL
existing values (including `m1_5c_announcement_sent` -- a prior draft dropped
it). The index is recreated inside the migration. Idempotent.
"""

from __future__ import annotations

import aiosqlite
import pytest

from scout.db import Database

_ALL_OUTCOMES = [
    "sent",
    "blocked_eligibility",
    "blocked_cooldown",
    "dispatch_failed",
    "announcement_sent",
    "m1_5c_announcement_sent",
    "blocked_dedup_24h",
]


@pytest.mark.asyncio
async def test_dedup_migration_preserves_all_outcome_values(tmp_path):
    """The rebuilt CHECK preserves ALL 6 prior values + blocked_dedup_24h.

    Regression guard: a prior draft dropped m1_5c_announcement_sent."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "SELECT sql FROM sqlite_master " "WHERE type='table' AND name='tg_alert_log'"
    )
    table_sql = (await cur.fetchone())[0]
    for v in _ALL_OUTCOMES:
        assert v in table_sql, f"outcome value missing from CHECK: {v}"
    assert "m1_5c_announcement_sent" in table_sql  # explicit regression guard
    await db.close()


@pytest.mark.asyncio
async def test_dedup_migration_inserts_each_value(tmp_path):
    """A row with each of the 7 allowed outcome values inserts cleanly."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    for v in _ALL_OUTCOMES:
        await db._conn.execute(
            "INSERT INTO tg_alert_log "
            "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
            "VALUES (NULL, 'gainers_early', 't', "
            "'2026-05-30T00:00:00+00:00', ?)",
            (v,),
        )
    await db._conn.commit()
    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_alert_log")
    assert (await cur.fetchone())[0] == len(_ALL_OUTCOMES)
    await db.close()


@pytest.mark.asyncio
async def test_dedup_migration_rejects_invalid_outcome(tmp_path):
    """An outcome value outside the widened CHECK is still rejected."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    with pytest.raises(aiosqlite.IntegrityError):
        await db._conn.execute(
            "INSERT INTO tg_alert_log "
            "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
            "VALUES (NULL, 'gainers_early', 't', "
            "'2026-05-30T00:00:00+00:00', 'not_a_real_outcome')",
        )
    await db.close()


@pytest.mark.asyncio
async def test_dedup_migration_idempotent_and_index_present(tmp_path):
    """Re-initializing (re-running the migration) is a no-op and preserves
    the widened CHECK + the token index recreated inside the rebuild."""
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db.close()

    db2 = Database(tmp_path / "t.db")
    await db2.initialize()  # second run; guard short-circuits
    cur = await db2._conn.execute(
        "SELECT sql FROM sqlite_master " "WHERE type='table' AND name='tg_alert_log'"
    )
    table_sql = (await cur.fetchone())[0]
    for v in _ALL_OUTCOMES:
        assert v in table_sql
    cur = await db2._conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='index' AND name='idx_tg_alert_log_token'"
    )
    assert (await cur.fetchone()) is not None
    await db2.close()
