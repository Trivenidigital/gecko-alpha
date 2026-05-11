#!/usr/bin/env python3
"""gecko_audit_snapshot - Phase B daily snapshot CLI.

Run by systemd timer gecko-audit-snapshot.timer at 04:00 UTC daily. Captures
volume_history_cg rows for slow_burn-detected coin_ids in the soak window
into the non-pruned audit_volume_snapshot_phase_b table.

On success: writes atomic heartbeat file (timestamp), exits 0. Watchdog
service gecko-audit-snapshot-watchdog.timer (10:00 UTC) checks heartbeat
freshness and alerts to Telegram if stale.

Exit codes:
    0 = success (rows captured + heartbeat written)
    2 = misconfiguration (DB path missing, bad ISO timestamps, etc.)
    3 = runtime error (DB error, lock contention, write failure)

Idempotency: ON CONFLICT DO NOTHING on (coin_id, recorded_at). Safe to run
multiple times per day.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog

# Make scout package importable when running from scripts/
sys.path.insert(0, str(Path(__file__).parent.parent))

from scout.audit.snapshot import snapshot_volume_history_for_phase_b
from scout.db import Database

logger = structlog.get_logger(__name__)


def _atomic_heartbeat_write(heartbeat_path: Path) -> None:
    """Write unix timestamp to heartbeat file atomically (.tmp + os.replace pattern).

    Matches the discipline of scripts/gecko-backup-rotate.sh:95-101: truncate-
    then-write exposes a 0-byte file to concurrent readers; kernel-atomic
    rename within same filesystem is the safe path.
    """
    heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = heartbeat_path.with_suffix(heartbeat_path.suffix + f".tmp.{os.getpid()}")
    tmp_path.write_text(str(int(datetime.now(timezone.utc).timestamp())))
    os.replace(tmp_path, heartbeat_path)


async def _run(args: argparse.Namespace) -> int:
    db = Database(args.db_path)
    try:
        await db.connect()
        # NOTE: db.connect() runs the migration chain itself; pipeline restart
        # in Task 7 Step 5 is for early observability of migration errors, not
        # a correctness prerequisite. CLI is self-migrating.
        rows, coin_ids = await snapshot_volume_history_for_phase_b(
            db,
            soak_start_iso=args.soak_start,
            soak_end_iso=args.soak_end,
        )
        logger.info(
            "audit_snapshot_cli_completed",
            rows_captured=rows,
            coin_ids_covered=coin_ids,
            db_path=args.db_path,
            heartbeat_file=args.heartbeat_file,
        )
    finally:
        await db.close()

    _atomic_heartbeat_write(Path(args.heartbeat_file))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, help="Path to scout.db")
    parser.add_argument(
        "--soak-start",
        required=True,
        help="ISO-8601 UTC timestamp for soak window start",
    )
    parser.add_argument(
        "--soak-end",
        required=True,
        help="ISO-8601 UTC timestamp for soak window end",
    )
    parser.add_argument(
        "--heartbeat-file",
        required=True,
        help="Path to atomic heartbeat file",
    )
    args = parser.parse_args()

    # Validate ISO timestamps
    try:
        datetime.fromisoformat(args.soak_start)
        datetime.fromisoformat(args.soak_end)
    except ValueError as e:
        print(f"ERROR: invalid ISO-8601 timestamp: {e}", file=sys.stderr)
        return 2

    if not Path(args.db_path).exists():
        print(f"ERROR: DB path does not exist: {args.db_path}", file=sys.stderr)
        return 2

    try:
        return asyncio.run(_run(args))
    except Exception as e:
        logger.exception("audit_snapshot_cli_failed", err=str(e), err_type=type(e).__name__)
        return 3


if __name__ == "__main__":
    sys.exit(main())
