"""Manual kill-switch CLI.

Usage:
    python -m scout.live.cli_kill --on "reason here"
    python -m scout.live.cli_kill --off
    python -m scout.live.cli_kill --status

Triggers / clears / inspects the live kill switch. Uses the ``DB_PATH`` env
var when set, otherwise the ``scout.db`` default (matching
``Settings.DB_PATH``). Suitable for on-call manual halt when shadow/live
trading misbehaves.

Note on DB path resolution: we deliberately avoid instantiating
:class:`scout.config.Settings` here because it requires ``TELEGRAM_*`` and
``ANTHROPIC_API_KEY`` env vars. A manual kill CLI should not depend on a
fully-configured alerting/LLM stack — on-call might be halting trading
precisely because something upstream of those is broken. Reading ``DB_PATH``
directly keeps the CLI's failure modes narrow to the database itself.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

import structlog

from scout.db import Database
from scout.live.kill_switch import KillSwitch, compute_kill_duration

log = structlog.get_logger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli_kill",
        description="Manually trigger / clear / inspect the live kill switch.",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--on", metavar="REASON", help="trigger the kill with REASON"
    )
    group.add_argument(
        "--off", action="store_true", help="clear an active kill"
    )
    group.add_argument(
        "--status", action="store_true", help="print current state"
    )
    return parser


def _resolve_db_path() -> str:
    """Return the DB path from env or fall back to the Settings default."""
    env_path = os.environ.get("DB_PATH")
    if env_path:
        return env_path
    return "scout.db"


async def main() -> int:
    args = _build_parser().parse_args()
    db = Database(_resolve_db_path())
    await db.initialize()
    try:
        ks = KillSwitch(db)
        if args.on is not None:
            kid, won = await ks.trigger(
                triggered_by="manual",
                reason=args.on,
                duration=compute_kill_duration(datetime.now(timezone.utc)),
            )
            print(f"kill_event_id={kid} won={won}")
            return 0
        if args.off:
            await ks.clear(cleared_by="manual")
            print("cleared")
            return 0
        if args.status:
            state = await ks.is_active()
            if state is None:
                print("status=inactive")
            else:
                print(
                    f"status=active kill_event_id={state.kill_event_id} "
                    f"killed_until={state.killed_until.isoformat()} "
                    f"triggered_by={state.triggered_by} "
                    f"reason={state.reason}"
                )
            return 0
    finally:
        await db.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
