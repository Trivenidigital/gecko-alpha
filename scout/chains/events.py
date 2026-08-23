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
    # ORDER MATTERS: fold the derived substrate BEFORE inserting the event.
    #
    # The two writes share a transaction so they commit together. The ordering
    # is what makes the FAILURE path safe, and it replaces an earlier attempt
    # that wrapped both in a SAVEPOINT. That attempt was wrong: `conn` is
    # shared by concurrently scheduled tasks (_pipeline_loop, run_chain_tracker
    # and the narrative agent all emit), savepoints are a STACK rather than a
    # per-coroutine scope, and interleaved emits do not nest LIFO. A failing
    # emit's ROLLBACK TO would discard a SIBLING emit's insert while keeping
    # its own -- and unique savepoint names do not help, because rolling back
    # to an outer savepoint still undoes the inner coroutine's work.
    #
    # With the fold first, neither failure can understate history:
    #   * fold raises  -> nothing of ours is pending; identical to the
    #     behaviour before the substrate existed.
    #   * insert raises -> the fold may be committed by a sibling's commit,
    #     leaving a first_seen for an event row that never landed. That records
    #     "we observed this token at time T", which is TRUE -- we tried to emit
    #     for it -- and the writer only ever LOWERS the minimum, so it cannot
    #     push first_seen later than the truth. Later is the harmful direction:
    #     it silently understates how early we saw a token.
    #
    # MIN semantics, so a replayed or out-of-order event still lowers the
    # minimum correctly rather than being dropped as a duplicate.
    await db.record_signal_first_seen(token_id, now)
    cursor = await conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (token_id, pipeline, event_type, json.dumps(event_data), source_module, now),
    )
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
