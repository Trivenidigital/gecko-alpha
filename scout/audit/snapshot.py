"""BL-NEW-AUDIT-SNAPSHOT: Phase B audit-time snapshot of volume_history_cg.

Captures `volume_history_cg` rows for slow_burn-detected coin_ids before the
rolling 7-day prune in scout/spikes/detector.py:55-57 deletes them. Output
table audit_volume_snapshot_phase_b is not subject to prune; data preserved
through D+14 evaluation 2026-05-24.

Idempotent: ON CONFLICT (coin_id, recorded_at) DO NOTHING per UNIQUE constraint.
Multiple daily runs do not duplicate rows.
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from scout.db import Database

logger = structlog.get_logger(__name__)


# Disk gate configuration. Path is overridable via env var so non-VPS
# environments (Windows / dev / CI) can target an existing path. VPS default
# is `/root` (where scout.db lives). If the path does not exist (OSError),
# the gate logs and skips — gate is a VPS-runtime safeguard, not a test gate.
DISK_GATE_PATH = os.environ.get("GECKO_AUDIT_DISK_GATE_PATH", "/root")
DISK_GATE_THRESHOLD_GB = float(
    os.environ.get("GECKO_AUDIT_DISK_GATE_THRESHOLD_GB", "10")
)


async def snapshot_volume_history_for_phase_b(
    db: "Database",
    soak_start_iso: str,
    soak_end_iso: str,
) -> tuple[int, int]:
    """Copy volume_history_cg rows for slow_burn-detected coin_ids into the
    non-pruned audit snapshot table.

    Args:
        db: Connected Database instance.
        soak_start_iso: ISO-8601 UTC timestamp marking start of soak window
            (used to filter slow_burn_candidates.detected_at).
        soak_end_iso: ISO-8601 UTC timestamp marking end of soak window.

    Returns:
        (rows_captured, distinct_coin_ids_covered) tuple. rows_captured counts
        new rows actually inserted (ON CONFLICT DO NOTHING returns 0 for
        duplicates). distinct_coin_ids_covered is the size of the slow_burn
        cohort whose rows were attempted.
    """
    if db._conn is None:
        raise RuntimeError("Database not initialized.")

    snapshotted_at = datetime.now(timezone.utc).isoformat()

    # 1. Get the slow_burn cohort coin_ids for the soak window
    cur = await db._conn.execute(
        "SELECT DISTINCT coin_id FROM slow_burn_candidates "
        "WHERE datetime(detected_at) >= datetime(?) "
        "AND datetime(detected_at) < datetime(?)",
        (soak_start_iso, soak_end_iso),
    )
    coin_id_rows = await cur.fetchall()
    coin_ids = [row[0] for row in coin_id_rows]

    if not coin_ids:
        logger.info(
            "audit_snapshot_empty_cohort",
            soak_start=soak_start_iso,
            soak_end=soak_end_iso,
        )
        return (0, 0)

    # 2. Pre-INSERT estimate (R6 "log-before-rm" pattern from gecko-backup-rotate).
    # Compute expected row count BEFORE writing so partial-failure post-mortems can
    # reconstruct intent. Cheap: bounded by cohort size, runs once.
    placeholders_all = ",".join("?" * len(coin_ids))
    est_cur = await db._conn.execute(
        f"SELECT COUNT(*) FROM volume_history_cg WHERE coin_id IN ({placeholders_all})",
        coin_ids,
    )
    est_row = await est_cur.fetchone()
    estimated_source_rows = int(est_row[0]) if est_row else 0
    logger.info(
        "audit_snapshot_starting",
        coin_ids_count=len(coin_ids),
        estimated_source_rows=estimated_source_rows,
        soak_start=soak_start_iso,
        soak_end=soak_end_iso,
        snapshotted_at=snapshotted_at,
    )

    # 2b. Disk pre-flight (R2-C1 hard gate, post-script-start re-check).
    # The bash wrapper does a pre-run check (catches deploy-time disk-low). This
    # check catches mid-run drift: pipeline may have written a large batch in
    # the seconds between bash-wrapper-start and Python-INSERT-start, eating
    # the slack. Re-verify immediately before chunk loop. <10G free → abort
    # cleanly; heartbeat file is NOT updated → watchdog at 10:00 UTC alerts
    # via existing direct-curl Telegram path.
    #
    # If GECKO_AUDIT_DISK_GATE_PATH points at a path that doesn't exist
    # (Windows dev, CI without /root), the gate logs and skips — gate is a
    # production safeguard, not a test isolation mechanism.
    try:
        free_bytes = shutil.disk_usage(DISK_GATE_PATH).free
        free_gb = free_bytes / 1_000_000_000
        if free_gb < DISK_GATE_THRESHOLD_GB:
            logger.error(
                "audit_snapshot_disk_gate_failed_at_insert_time",
                path=DISK_GATE_PATH,
                free_gb=round(free_gb, 2),
                threshold_gb=DISK_GATE_THRESHOLD_GB,
                coin_ids_count=len(coin_ids),
                estimated_source_rows=estimated_source_rows,
            )
            raise RuntimeError(
                f"Disk gate failed at INSERT time: {free_gb:.2f}G free at "
                f"{DISK_GATE_PATH}, need {DISK_GATE_THRESHOLD_GB}G. "
                f"Cohort={len(coin_ids)} coin_ids, "
                f"estimated_rows={estimated_source_rows}. "
                f"Heartbeat not updated; watchdog at 10:00 UTC will alert."
            )
    except OSError as e:
        # Path does not exist (e.g., /root on Windows dev). Gate is a VPS-runtime
        # safeguard — skip with a warning so non-VPS environments work cleanly.
        logger.warning(
            "audit_snapshot_disk_gate_path_unavailable",
            path=DISK_GATE_PATH,
            err=str(e),
            note=(
                "disk gate skipped; set GECKO_AUDIT_DISK_GATE_PATH to a "
                "valid path for VPS runtime"
            ),
        )

    # 3. Copy matching volume_history_cg rows with ON CONFLICT DO NOTHING.
    # SQLite parameter limit safety: chunk if cohort > 500 coin_ids.
    # Per-chunk commit (R2-M2 amendment): a single multi-chunk transaction
    # would hold SQLite's write lock for the duration of all chunks, blocking
    # the pipeline's `record_volume` writer (60s cadence) for seconds-to-minutes.
    # Per-chunk commit keeps each lock-hold to <1s, letting the pipeline
    # interleave its own writes. The CLI runs in a separate process; the
    # in-process `_txn_lock` pattern that the pipeline uses for in-process
    # coordination does NOT apply here. Cross-process serialization is SQLite's job.
    CHUNK = 500
    total_inserted = 0
    for i in range(0, len(coin_ids), CHUNK):
        chunk = coin_ids[i : i + CHUNK]
        placeholders = ",".join("?" * len(chunk))
        # INSERT OR IGNORE for SQLite ON CONFLICT compatibility on
        # UNIQUE-constrained insert. cur.rowcount returns rows actually inserted.
        cur_insert = await db._conn.execute(
            f"""INSERT OR IGNORE INTO audit_volume_snapshot_phase_b
                (coin_id, symbol, name, volume_24h, market_cap, price,
                 recorded_at, snapshotted_at)
                SELECT coin_id, symbol, name, volume_24h, market_cap, price,
                       recorded_at, ?
                FROM volume_history_cg
                WHERE coin_id IN ({placeholders})""",
            (snapshotted_at, *chunk),
        )
        chunk_inserted = cur_insert.rowcount if cur_insert.rowcount is not None else 0
        total_inserted += chunk_inserted
        # Per-chunk commit releases SQLite write lock between chunks.
        await db._conn.commit()
        logger.debug(
            "audit_snapshot_chunk_committed",
            chunk_index=i // CHUNK,
            chunk_size=len(chunk),
            chunk_inserted=chunk_inserted,
        )

    logger.info(
        "audit_snapshot_completed",
        rows_captured=total_inserted,
        coin_ids_covered=len(coin_ids),
        soak_start=soak_start_iso,
        soak_end=soak_end_iso,
        snapshotted_at=snapshotted_at,
    )
    return (total_inserted, len(coin_ids))
