"""BL-050 — Qualifier transition state for first_signal paper trades.

Replaces the historical current-state check in trade_first_signals with a
transition-into-qualifier check backed by a persisted table. Rationale and
acceptance criteria live in
docs/superpowers/specs/2026-04-19-bl050-paper-trade-edge-detection-design.md.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import structlog

from scout.db import Database

log = structlog.get_logger()


async def classify_transitions(
    db: Database,
    *,
    signal_type: str,
    current_token_ids: set[str],
    now: datetime,
    exit_grace_hours: int,
) -> dict[str, str | None]:
    """Classify current_token_ids into transitions (returned) and continuations (not).

    Upserts ALL current_token_ids unconditionally. Returns a mapping of transitioned
    token_id → prior `last_qualified_at` ISO string (or None for tokens with no
    prior row). Callers iterate keys for membership and read values for
    observability (`prior_last_qualified_at`, `elapsed_since_prior_hours`).

    A token is a transition iff it had NO prior row, OR its prior
    `last_qualified_at` is strictly older than `now - exit_grace_hours`.

    Boundary convention: prior last_qualified_at == (now - exit_grace_hours)
    counts as continuation (inclusive).

    Empty input early-returns {} without touching the DB or the txn lock.

    Error policy: aiosqlite errors propagate. Caller is REQUIRED to wrap
    invocation in try/except and fail-closed for the cycle.
    """
    if not current_token_ids:
        return {}

    if db._conn is None or db._txn_lock is None:
        raise RuntimeError(
            "Database not initialized — classify_transitions() called before "
            "Database.initialize()."
        )

    threshold = (now - timedelta(hours=exit_grace_hours)).isoformat()
    now_iso = now.isoformat()

    async with db._txn_lock:
        # Read all existing rows for these tokens in one query.
        placeholders = ",".join("?" for _ in current_token_ids)
        ids_list = list(current_token_ids)
        cur = await db._conn.execute(
            f"SELECT token_id, last_qualified_at FROM signal_qualifier_state "
            f"WHERE signal_type = ? AND token_id IN ({placeholders})",
            (signal_type, *ids_list),
        )
        existing = {row[0]: row[1] for row in await cur.fetchall()}

        transitions: dict[str, str | None] = {}
        for tid in current_token_ids:
            prior_last = existing.get(tid)
            if prior_last is None:
                transitions[tid] = None
                continue
            # Compare ISO-8601 strings via datetime() wrapper — per PR #24,
            # raw string comparison breaks on any format drift (timezone
            # offset, microsecond precision). Push the comparison into SQL.
            cmp = await db._conn.execute(
                "SELECT datetime(?) > datetime(?)",
                (threshold, prior_last),
            )
            cmp_row = await cmp.fetchone()
            if cmp_row and cmp_row[0]:
                # threshold is strictly greater than prior_last → transition.
                # Record prior for observability.
                transitions[tid] = prior_last

        # Upsert every token. first_qualified_at:
        #   - new row: now
        #   - transition (prior outside grace): now (reset)
        #   - continuation: preserved via UPDATE of last_qualified_at only
        for tid in current_token_ids:
            if tid in transitions:
                # transition or brand-new: first = now, last = now
                await db._conn.execute(
                    "INSERT INTO signal_qualifier_state "
                    "(signal_type, token_id, first_qualified_at, last_qualified_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(signal_type, token_id) DO UPDATE SET "
                    "first_qualified_at=excluded.first_qualified_at, "
                    "last_qualified_at=excluded.last_qualified_at",
                    (signal_type, tid, now_iso, now_iso),
                )
            else:
                # continuation: preserve first_qualified_at, bump last_qualified_at
                await db._conn.execute(
                    "UPDATE signal_qualifier_state "
                    "SET last_qualified_at = ? "
                    "WHERE signal_type = ? AND token_id = ?",
                    (now_iso, signal_type, tid),
                )
        await db._conn.commit()

    return transitions


async def prune_stale_qualifiers(
    db: Database,
    *,
    now: datetime,
    retention_hours: int,
) -> int:
    """Delete rows where datetime(last_qualified_at) < datetime(now - retention).

    Returns the number of rows deleted. Acquires db._txn_lock.

    retention_hours must be > 0; callers pass settings.QUALIFIER_PRUNE_RETENTION_HOURS
    which is enforced > 0 by the Settings model_validator. A defensive check here
    catches programming errors (zero/negative literal in a caller).

    Read-only SELECT COUNT first so a clean table doesn't open a write transaction.
    """
    if retention_hours <= 0:
        raise ValueError(f"retention_hours must be > 0, got {retention_hours}")

    if db._conn is None or db._txn_lock is None:
        raise RuntimeError(
            "Database not initialized — prune_stale_qualifiers() called before "
            "Database.initialize()."
        )

    threshold = (now - timedelta(hours=retention_hours)).isoformat()

    async with db._txn_lock:
        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM signal_qualifier_state "
            "WHERE datetime(last_qualified_at) < datetime(?)",
            (threshold,),
        )
        count_row = await cur.fetchone()
        count = count_row[0] if count_row else 0
        if count == 0:
            return 0

        cursor = await db._conn.execute(
            "DELETE FROM signal_qualifier_state "
            "WHERE datetime(last_qualified_at) < datetime(?)",
            (threshold,),
        )
        await db._conn.commit()
        return cursor.rowcount
