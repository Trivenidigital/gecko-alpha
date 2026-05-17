"""Watchdog for chain-pattern availability and active-chain freshness."""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scout.chains.patterns import evaluate_condition


def _parse_env_bool(path: Path, key: str, default: bool) -> bool:
    if not path.exists():
        return default
    prefix = f"{key}="
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not stripped.startswith(prefix):
            continue
        value = stripped.split("=", 1)[1].strip().strip("'\"").lower()
        return value in {"1", "true", "yes", "on"}
    return default


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _load_protected_anchor_steps(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT steps_json
           FROM chain_patterns
           WHERE is_active = 1 AND is_protected_builtin = 1"""
    ).fetchall()
    steps: list[dict[str, Any]] = []
    for row in rows:
        pattern_steps = json.loads(row["steps_json"])
        first = next(
            (step for step in pattern_steps if int(step["step_number"]) == 1),
            None,
        )
        if first is None:
            continue
        steps.append(
            {
                "pipeline": None,
                "event_type": first["event_type"],
                "condition": first.get("condition"),
            }
        )
    return steps


def _count_recent_anchor_events(
    conn: sqlite3.Connection,
    *,
    since: datetime,
) -> int:
    steps = _load_protected_anchor_steps(conn)
    if not steps:
        return 0
    event_types = sorted({step["event_type"] for step in steps})
    placeholders = ",".join("?" for _ in event_types)
    rows = conn.execute(
        f"""SELECT pipeline, event_type, event_data
            FROM signal_events
            WHERE created_at >= ?
              AND event_type IN ({placeholders})""",
        (since.isoformat(), *event_types),
    ).fetchall()

    count = 0
    for row in rows:
        data = json.loads(row["event_data"])
        for step in steps:
            if step["event_type"] != row["event_type"]:
                continue
            if step["pipeline"] is not None and step["pipeline"] != row["pipeline"]:
                continue
            if evaluate_condition(step["condition"], data):
                count += 1
                break
    return count


def check_chain_anchor_health(
    db_path: str | Path,
    *,
    env_path: str | Path | None = None,
    anchor_window_hours: float = 24.0,
    active_stale_hours: float = 24.0,
) -> dict[str, Any]:
    """Return chain-anchor health as a JSON-serializable dict."""
    env = Path(env_path) if env_path is not None else Path(".env")
    if not _parse_env_bool(env, "CHAINS_ENABLED", True):
        return {"ok": True, "status": "disabled", "reasons": []}

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=anchor_window_hours)
    reasons: list[str] = []
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        active_protected = conn.execute(
            """SELECT COUNT(*)
               FROM chain_patterns
               WHERE is_active = 1 AND is_protected_builtin = 1"""
        ).fetchone()[0]
        if active_protected == 0:
            reasons.append("no_active_protected_patterns")

        recent_anchor_events = _count_recent_anchor_events(conn, since=since)
        row = conn.execute("SELECT MAX(anchor_time) FROM active_chains").fetchone()
        max_anchor = _parse_time(row[0] if row else None)
        if recent_anchor_events > 0:
            if max_anchor is None:
                reasons.append("active_chains_missing")
            elif (now - max_anchor).total_seconds() / 3600.0 > active_stale_hours:
                reasons.append("active_chains_stale")

    return {
        "ok": not reasons,
        "status": "ok" if not reasons else "alert",
        "active_protected_patterns": active_protected,
        "recent_anchor_events": recent_anchor_events,
        "active_chains_max_anchor_time": max_anchor.isoformat() if max_anchor else None,
        "reasons": reasons,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--env", type=Path, default=Path(".env"))
    parser.add_argument("--anchor-window-hours", type=float, default=24.0)
    parser.add_argument("--active-stale-hours", type=float, default=24.0)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    result = check_chain_anchor_health(
        args.db,
        env_path=args.env,
        anchor_window_hours=args.anchor_window_hours,
        active_stale_hours=args.active_stale_hours,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
