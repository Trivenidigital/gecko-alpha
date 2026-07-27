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
    db: Database | None,
    token_id: str,
    pipeline: str,
    event_type: str,
    event_data: dict,
    source_module: str,
    *,
    conn=None,
) -> int:
    """Append a signal event. Returns the new event row id.

    F2 transaction-awareness (change #2): when ``conn`` is provided the caller
    is inside an open ``db.transaction()``; the INSERT runs on that connection
    and is NOT committed here — the caller's manager owns COMMIT/ROLLBACK, so
    the event row is atomic with the caller's other writes (no early commit of
    the outer transaction). When ``conn`` is None the write goes through the
    disciplined single-write path (its own locked transaction), so no bare
    commit is ever issued on the shared connection.
    """
    if pipeline not in ("narrative", "memecoin"):
        raise ValueError(f"Invalid pipeline: {pipeline!r}")

    now = datetime.now(timezone.utc).isoformat()
    sql = (
        "INSERT INTO signal_events "
        "(token_id, pipeline, event_type, event_data, source_module, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    params = (
        token_id,
        pipeline,
        event_type,
        json.dumps(event_data),
        source_module,
        now,
    )
    if conn is not None:
        # Inside a caller's manager transaction — write only, no commit.
        cursor = await conn.execute(sql, params)
        eid = cursor.lastrowid
    else:
        if db is None or db._conn is None:
            raise RuntimeError("Database not initialized")
        async with db.transaction() as txn_conn:
            cursor = await txn_conn.execute(sql, params)
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


async def prune_old_events(db: Database, retention_days: int, *, conn=None) -> int:
    """Delete events older than retention_days. Returns rows deleted.

    F2 transaction-awareness: with ``conn`` (inside a caller's transaction) the
    DELETE runs on that connection WITHOUT committing; standalone it uses the
    disciplined single-write path so no bare commit is issued.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    sql = "DELETE FROM signal_events WHERE created_at < ?"
    if conn is not None:
        cursor = await conn.execute(sql, (cutoff,))
        return cursor.rowcount or 0
    if db._conn is None:
        raise RuntimeError("Database not initialized")
    return await db.execute_write(sql, (cutoff,)) or 0


async def safe_emit(
    db: Database,
    token_id: str,
    pipeline: str,
    event_type: str,
    event_data: dict,
    source_module: str,
    *,
    conn=None,
) -> int | None:
    """Call emit_event, log and swallow any exception.

    Use this from existing pipeline modules so chain tracking failures
    never break the main pipeline. When `CHAINS_ENABLED=False` this is a
    total no-op — no DB row is inserted. Pass ``conn`` when already inside a
    ``db.transaction()`` so the event insert joins that transaction without an
    early commit (F2 change #2).
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
            db, token_id, pipeline, event_type, event_data, source_module, conn=conn
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
