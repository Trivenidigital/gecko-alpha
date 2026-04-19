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


async def prune_old_events(db: Database, retention_days: int) -> int:
    """Delete events older than retention_days. Returns rows deleted."""
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    cursor = await conn.execute(
        "DELETE FROM signal_events WHERE created_at < ?", (cutoff,)
    )
    await conn.commit()
    return cursor.rowcount or 0


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
