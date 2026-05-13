"""Watchdog for Minara command-emission persistence parity."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from scripts.backfill_minara_alert_emissions import parse_minara_emission_line


def _load_journal_events(journal_path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        parsed = parse_minara_emission_line(line)
        if parsed is not None:
            events.append(parsed)
    return events


def _load_persisted_event_ids(
    db_path: Path,
    source_event_ids: set[str],
) -> set[str]:
    if not source_event_ids:
        return set()
    placeholders = ",".join("?" for _ in source_event_ids)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "SELECT source_event_id FROM minara_alert_emissions "
            f"WHERE source_event_id IN ({placeholders})",
            tuple(sorted(source_event_ids)),
        )
        return {str(row[0]) for row in cur.fetchall()}


def check_persistence_parity(
    db_path: Path,
    journal_path: Path,
    *,
    tolerance: int = 0,
) -> dict[str, Any]:
    """Compare journal Minara emissions with persisted DB rows by event ID."""
    events = _load_journal_events(journal_path)
    if not events:
        return {
            "ok": True,
            "journal_count": 0,
            "db_count": 0,
            "deficit": 0,
            "missing_source_event_ids": [],
            "since": None,
        }

    since = min(str(event["emitted_at"]) for event in events)
    source_event_ids = {str(event["source_event_id"]) for event in events}
    try:
        persisted_event_ids = _load_persisted_event_ids(db_path, source_event_ids)
        db_error = None
    except sqlite3.Error as exc:
        persisted_event_ids = set()
        db_error = f"{type(exc).__name__}: {exc}"

    missing_event_ids = sorted(source_event_ids - persisted_event_ids)
    deficit = max(0, len(missing_event_ids) - tolerance)
    result: dict[str, Any] = {
        "ok": deficit == 0 and db_error is None,
        "journal_count": len(events),
        "db_count": len(persisted_event_ids),
        "deficit": deficit,
        "missing_source_event_ids": missing_event_ids[:20],
        "since": since,
    }
    if db_error is not None:
        result["db_error"] = db_error
    return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check that minara_alert_command_emitted journal events are "
            "represented in minara_alert_emissions."
        )
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--journal", required=True, type=Path)
    parser.add_argument("--tolerance", default=0, type=int)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    result = check_persistence_parity(
        args.db,
        args.journal,
        tolerance=args.tolerance,
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
