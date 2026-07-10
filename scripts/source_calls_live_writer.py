#!/usr/bin/env python3
"""Periodic writer for the `source_calls` ledger.

Runs both `backfill_source_calls` (insert/upsert upstream TG/X rows) and
`refresh_source_call_outcomes` (resolve forward-window prices for existing
rows). Designed to be called from cron at a cadence well inside the lag
watchdog's freshness SLO so the watchdog has fresh writes to monitor.

Idempotent — `backfill_source_calls` UPSERTs by
(source_type, source_event_id), and `refresh_source_call_outcomes` UPDATEs
in place. Repeated runs do not duplicate rows.

Operator-visible alerting is intentionally NOT here. The lag watchdog
(scripts/source-calls-lag-watchdog.sh) owns Telegram dispatch; the writer
emits stdout JSON for journal capture.

Optional `--heartbeat-file PATH` touches the given path on success so the
lag watchdog can detect writer-cron outages independently of upstream
traffic. Touch failures are logged as a structured warning but do NOT
fail the writer — the absent / stale heartbeat surfaces to the watchdog
on its next tick as `writer_heartbeat_missing` / `writer_stale`.

Exit codes:
  0 — success (zero inserts/updates is still success when upstream is empty)
  1 — DB missing or runtime error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aiosqlite
import structlog

from scout.source_quality.ledger import (
    backfill_source_calls,
    refresh_source_call_outcomes,
)


async def _run(db_path: str) -> dict:
    started = datetime.now(timezone.utc)
    async with aiosqlite.connect(db_path) as conn:
        conn.row_factory = aiosqlite.Row
        backfill = await backfill_source_calls(conn)
        refresh = await refresh_source_call_outcomes(conn)
    finished = datetime.now(timezone.utc)
    return {
        "backfill": backfill,
        "refresh": refresh,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_sec": round((finished - started).total_seconds(), 3),
    }


def _touch_heartbeat(path: Path) -> None:
    """Best-effort heartbeat touch. Failures log but do not propagate.

    Per CLAUDE.md memory `feedback_resilience_layered_failure_modes`:
    failing to touch is observable to the lag watchdog (file mtime stays
    stale -> alert fires), so best-effort here does NOT swallow the
    failure — it relocates the operator-visible surface to the watchdog
    read-path.
    """
    log = structlog.get_logger()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(exist_ok=True)
    except OSError as err:
        log.warning(
            "source_calls_heartbeat_touch_failed",
            path=str(path),
            errno=err.errno,
            error=str(err),
        )


def _configure_logging() -> None:
    """Route structlog output to stderr so this CLI's stdout is JSON-only.
    Without this, scout.source_quality.ledger's log.info() calls would mix
    console-rendered log lines into stdout and break downstream JSON parsing.

    Called ONLY from the ``__main__`` / cron entrypoint — NOT at import time
    (INF-03). Configuring structlog at module scope is a GLOBAL, process-wide
    mutation: importing this module in a unit test would reconfigure every other
    test's logger and silently empty their captured output. Keeping it here makes
    the import side-effect-free."""
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db")
    parser.add_argument(
        "--heartbeat-file",
        default=None,
        help=(
            "Optional. Touch this file on successful run (mtime = now). "
            "Consumed by source-calls-lag-watchdog to detect writer cron "
            "outages independently of upstream traffic."
        ),
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser()
    if not db_path.exists():
        print(
            json.dumps(
                {"ok": False, "error": "db_not_found", "db": str(db_path)},
                sort_keys=True,
            )
        )
        return 1

    try:
        result = asyncio.run(_run(str(db_path)))
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "runtime_error",
                    "detail": str(exc)[:200],
                    "db": str(db_path),
                },
                sort_keys=True,
            )
        )
        return 1

    result["ok"] = True
    if args.heartbeat_file:
        _touch_heartbeat(Path(args.heartbeat_file).expanduser())
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    _configure_logging()
    sys.exit(main())
