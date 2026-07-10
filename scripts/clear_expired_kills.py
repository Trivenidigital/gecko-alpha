#!/usr/bin/env python3
"""One-time / on-demand clear of an EXPIRED-but-latched live kill switch (LIVE-01).

Background: ``kill_events`` #1 (daily_loss_cap, killed_until 2026-06-07) never
cleared because ``KillSwitch.auto_clear_if_expired()`` had zero production
callers and ``is_active()`` ignored ``killed_until`` — latching the kill ~33
days and freezing the BL-055 shadow soak (Gate 1 rejected every open). LIVE-01
wires the auto-clear into the evaluator tick + engine boot; this script is the
operator/deploy step to clear the already-latched row on prod WITHOUT waiting
for the next deploy tick.

Read-only by default (dry-run): reports whether an expired kill is latched and
what would be cleared, and touches nothing. Pass ``--apply`` to actually clear
it — stamps ``cleared_at`` + ``cleared_by='auto_expired'`` and nulls
``live_control.active_kill_event_id``, mirroring ``KillSwitch.clear`` exactly. A
fresh (not-yet-expired) kill is always left untouched.

Uses raw aiosqlite (NOT scout.db.Database) on purpose: opening Database would
run schema migrations, a side effect this targeted maintenance step must not
introduce on prod. The dry-run path issues no writes.

Deploy usage (srilu):
    python scripts/clear_expired_kills.py --db /root/gecko-alpha/scout.db          # preview
    python scripts/clear_expired_kills.py --db /root/gecko-alpha/scout.db --apply  # clear

Exit codes:
  0 — success (dry-run report, successful --apply clear, or nothing to clear)
  1 — DB missing or runtime error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite


def _parse_ts(raw: str) -> datetime:
    """Parse an ISO ``killed_until`` value into a tz-aware UTC datetime."""
    s = raw.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _find_active_kill(conn: aiosqlite.Connection, now: datetime) -> dict | None:
    """Return the currently-active (``cleared_at IS NULL``) kill with its expiry
    verdict, or ``None`` if nothing is active / the live tables are absent."""
    try:
        cur = await conn.execute(
            "SELECT ke.id, ke.killed_until, ke.reason, ke.triggered_by, "
            "       ke.triggered_at "
            "  FROM live_control AS lc "
            "  JOIN kill_events  AS ke ON ke.id = lc.active_kill_event_id "
            " WHERE lc.id = 1 AND ke.cleared_at IS NULL"
        )
        row = await cur.fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return None
        raise
    if row is None:
        return None
    killed_until = _parse_ts(row[1])
    expired = killed_until < now
    latched_hours = (
        round((now - killed_until).total_seconds() / 3600.0, 1) if expired else None
    )
    return {
        "kill_event_id": row[0],
        "killed_until": row[1],
        "reason": row[2],
        "triggered_by": row[3],
        "triggered_at": row[4],
        "expired": expired,
        "latched_hours_past_expiry": latched_hours,
    }


async def _apply_clear(
    conn: aiosqlite.Connection, kill_event_id: int, now: datetime
) -> None:
    """Clear the kill, mirroring ``KillSwitch.clear`` (cleared_by='auto_expired').

    ``auto_expired`` is one of the two values allowed by the ``kill_events``
    ``cleared_by`` CHECK constraint (the other is ``manual``); it matches the
    semantics of the in-process auto-clear this script substitutes for.
    """
    await conn.execute(
        "UPDATE live_control SET active_kill_event_id = NULL WHERE id = 1"
    )
    await conn.execute(
        "UPDATE kill_events SET cleared_at = ?, cleared_by = 'auto_expired' "
        "WHERE id = ?",
        (now.isoformat(), kill_event_id),
    )
    await conn.commit()


async def _run(db_path: str, *, apply: bool) -> dict:
    now = datetime.now(timezone.utc)
    async with aiosqlite.connect(db_path) as conn:
        found = await _find_active_kill(conn, now)
        if found is None:
            return {"ok": True, "action": "none", "reason": "no_active_kill"}
        if not found["expired"]:
            return {
                "ok": True,
                "action": "none",
                "reason": "kill_not_expired",
                **found,
            }
        if not apply:
            return {"ok": True, "action": "dry_run", "would_clear": True, **found}
        await _apply_clear(conn, found["kill_event_id"], now)
        return {"ok": True, "action": "cleared", **found}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Clear an expired-but-latched live kill switch (LIVE-01)."
    )
    parser.add_argument("--db", default="scout.db")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the clear. Default is a read-only dry-run.",
    )
    args = parser.parse_args(argv)

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
        result = asyncio.run(_run(str(db_path), apply=args.apply))
    except Exception as exc:  # pragma: no cover — defensive
        print(
            json.dumps(
                {"ok": False, "error": "runtime_error", "detail": str(exc)[:200]},
                sort_keys=True,
            )
        )
        return 1

    print(json.dumps(result, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
