"""Signal event emission + retrieval for the chain tracker.

Every module with a meaningful signal calls `emit_event()` exactly once at its
natural decision point. The event store is append-only — no deduplication.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import structlog

from scout.chains.models import ChainEvent
from scout.db import Database

logger = structlog.get_logger()


async def emit_event(
    db: Database,
    token_id: str,
    pipeline: str,
    event_type: str,
    event_data: dict,
    source_module: str,
) -> int:
    """Append a signal event. Returns the new event row id."""
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")
    if pipeline not in ("narrative", "memecoin"):
        raise ValueError(f"Invalid pipeline: {pipeline!r}")

    now = datetime.now(timezone.utc).isoformat()
    # The event insert and the derived-substrate fold are ONE unit.
    #
    # They must share a transaction: separate ones leave a window where the
    # event exists but the substrate does not, and because the writer only ever
    # LOWERS the minimum, a crash in that window makes this token's first_seen
    # permanently later than the truth with nothing to detect it.
    #
    # The SAVEPOINT is what makes the failure path safe, and it is not
    # decorative. `conn` is shared and this function did not previously have a
    # failure point between INSERT and commit; now it does. Without the
    # savepoint a raising fold leaves the INSERT pending on the connection,
    # `safe_emit` swallows the exception, and the NEXT emit's commit() launders
    # the orphaned event into the database -- manufacturing exactly the
    # committed-event-without-substrate-row divergence this code exists to
    # prevent. ROLLBACK TO undoes only this unit, so any outer pending work on
    # the shared connection survives (a bare conn.rollback() would discard it).
    await conn.execute("SAVEPOINT emit_event")
    try:
        cursor = await conn.execute(
            """INSERT INTO signal_events
               (token_id, pipeline, event_type, event_data, source_module, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                token_id,
                pipeline,
                event_type,
                json.dumps(event_data),
                source_module,
                now,
            ),
        )
        # MIN semantics, so a replayed or out-of-order event still lowers the
        # minimum correctly rather than being dropped as a duplicate.
        await db.record_signal_first_seen(token_id, now)
    except BaseException:
        await conn.execute("ROLLBACK TO emit_event")
        await conn.execute("RELEASE emit_event")
        raise
    await conn.execute("RELEASE emit_event")
    await conn.commit()
    eid = cursor.lastrowid
    logger.debug(
        "chain_event_emitted",
        event_id=eid,
        token_id=token_id,
        pipeline=pipeline,
        event_type=event_type,
        source_module=source_module,
    )
    return int(eid)


async def load_recent_events(db: Database, max_hours: float) -> list[ChainEvent]:
    """Load events from the last `max_hours`, oldest first."""
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_hours)).isoformat()
    async with conn.execute(
        """SELECT id, token_id, pipeline, event_type, event_data,
                  source_module, created_at
           FROM signal_events
           WHERE created_at >= ?
           ORDER BY created_at ASC""",
        (cutoff,),
    ) as cur:
        rows = await cur.fetchall()
    return [
        ChainEvent(
            id=row["id"],
            token_id=row["token_id"],
            pipeline=row["pipeline"],
            event_type=row["event_type"],
            event_data=json.loads(row["event_data"]),
            source_module=row["source_module"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )
        for row in rows
    ]


# `prune_old_events` used to live here. It was removed, not merely
# un-called: `signal_events` retention is owned by exactly ONE implementation,
# `Database.prune_signal_events`, driven from the hourly maintenance pass in
# scout/main.py::_run_hourly_maintenance. Leaving a second, identical DELETE
# reachable from the chains engine is the trap this repair exists to close —
# the prune was gated on a non-empty load_active_patterns() result and would
# have stopped silently the moment the pattern set emptied. Two owners for one
# table's retention is how the boundary drifts back apart. Do not re-add a
# prune here; extend the hourly pass instead.


async def safe_emit(
    db: Database,
    token_id: str,
    pipeline: str,
    event_type: str,
    event_data: dict,
    source_module: str,
) -> int | None:
    """Call emit_event, log and swallow any exception.

    Use this from existing pipeline modules so chain tracking failures
    never break the main pipeline. When `CHAINS_ENABLED=False` this is a
    total no-op — no DB row is inserted.
    """
    try:
        from scout.config import get_settings  # lazy import to avoid cycle

        settings = get_settings()
        if not getattr(settings, "CHAINS_ENABLED", False):
            return None
    except Exception:
        # If settings load fails we deliberately swallow so the
        # pipeline-side chain emitter doesn't crash. But silent
        # `return None` hid the config-load failure from operators —
        # log so journalctl shows the path was hit and why.
        logger.exception("chain_event_settings_load_failed")
        return None
    try:
        return await emit_event(
            db, token_id, pipeline, event_type, event_data, source_module
        )
    except Exception as exc:
        logger.warning(
            "chain_event_emit_failed",
            token_id=token_id,
            pipeline=pipeline,
            event_type=event_type,
            error=str(exc),
        )
        return None
