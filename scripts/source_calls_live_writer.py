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

# Route structlog output to stderr so this CLI's stdout is JSON-only.
# Without this, scout.source_quality.ledger's log.info() calls would mix
# console-rendered log lines into stdout and break downstream JSON parsing.
structlog.configure(
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)

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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db")
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
    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
