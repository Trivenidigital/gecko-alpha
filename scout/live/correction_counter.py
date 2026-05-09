"""BL-NEW-LIVE-HYBRID M1.5b: signal_venue_correction_count writer.

Closes V1 review's C2 finding (counter stuck at 0 forever — no writers).
The counter is read by approval_thresholds.should_require_approval Gate 1
(new-venue gate: < 30 consecutive_no_correction → require approval).

M1.5b's increment-on-fill semantic is a SIMPLIFICATION of design intent
(design says increment after 24h with no operator unwind). M1.5c's
reconciler may refine to true 24h-window logic.

Plan-stage R1+R2 finding C3: increments only on terminal status='filled'
(NOT 'partial' — PARTIALLY_FILLED can transition to CANCELED for IOC
orders). Plan-stage R1-I7: empty/None signal_type coerced to "unknown".
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from scout.db import Database

log = structlog.get_logger(__name__)


async def increment_consecutive(
    db: Database, signal_type: str | None, venue: str
) -> None:
    """Increment consecutive_no_correction by 1 for (signal_type, venue).

    Creates the row on first call (ON CONFLICT...DO UPDATE).

    Empty/None signal_type is coerced to "unknown" (R1-I7) — avoids a
    crash if a future dispatcher path emits empty signal_type.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    signal_type = signal_type or "unknown"
    now_iso = datetime.now(timezone.utc).isoformat()
    async with db._txn_lock:
        await db._conn.execute(
            """INSERT INTO signal_venue_correction_count
               (signal_type, venue, consecutive_no_correction, last_updated_at)
               VALUES (?, ?, 1, ?)
               ON CONFLICT (signal_type, venue) DO UPDATE SET
                  consecutive_no_correction = consecutive_no_correction + 1,
                  last_updated_at = excluded.last_updated_at""",
            (signal_type, venue, now_iso),
        )
        await db._conn.commit()
    log.info(
        "correction_counter_incremented",
        signal_type=signal_type,
        venue=venue,
    )


async def reset_on_correction(
    db: Database,
    signal_type: str | None,
    venue: str,
    correction_at: str,
) -> None:
    """Reset consecutive_no_correction to 0 + record last_corrected_at.

    Called from the operator-correction path (M1.5c when reconciler
    detects a 24h-window unwind; M1.5b operator can call directly via
    SQL for manual corrections).

    SEMANTIC ACKNOWLEDGMENT (R2 design-stage finding C2): a single reset
    zeros the ENTIRE consecutive_no_correction field for the
    (signal_type, venue) pair. Worked example: 30 fills → counter=30 →
    operator unwinds trade #31 → counter=0 → all 30 prior good fills
    lose their auto-clear-approval progress. This semantic matches the
    field name (consecutive_no_correction = "consecutive trades without
    correction") and matches V1's gate intent ("trust requires UNBROKEN
    streak"), but has UX consequence — runbook entry surfaces this.
    M1.5c reconciler may add total_fills_lifetime for dashboard
    telemetry that survives resets.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")
    signal_type = signal_type or "unknown"
    now_iso = datetime.now(timezone.utc).isoformat()
    async with db._txn_lock:
        await db._conn.execute(
            """INSERT INTO signal_venue_correction_count
               (signal_type, venue, consecutive_no_correction,
                last_corrected_at, last_updated_at)
               VALUES (?, ?, 0, ?, ?)
               ON CONFLICT (signal_type, venue) DO UPDATE SET
                  consecutive_no_correction = 0,
                  last_corrected_at = excluded.last_corrected_at,
                  last_updated_at = excluded.last_updated_at""",
            (signal_type, venue, correction_at, now_iso),
        )
        await db._conn.commit()
    log.info(
        "correction_counter_reset",
        signal_type=signal_type,
        venue=venue,
        correction_at=correction_at,
    )
