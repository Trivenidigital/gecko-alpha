"""One-time backfill of ledger_enrollment_evictions from the interim journal
export JSONL (scripts/ledger-eviction-export.sh output).

BL-NEW-LEDGER-EVICTION-DB-MARKER, REQUIRED 2026-07-02 (#406 ruling). The durable
DB eviction marker landed AFTER the eviction code + weekly journal export were
already live, so evictions between deploy-#2 and the marker deploy exist only in
the journal (and its size-rotated JSONL copy). This script explodes each
``ledger_enrollment_evicted`` record into per-token
``ledger_enrollment_evictions`` rows before journald rotation (~3wk clock) makes
the export lossy.

Input: the JSONL produced by scripts/ledger-eviction-export.sh — one journald
envelope per line (``journalctl -o json``), the structlog event living in
``.MESSAGE``. Raw structlog lines (no envelope) are also accepted. The export
file may carry duplicate lines (it dedups at READ time, not on append); this
script dedups on the durable (token_id, evicted_at) key.

Idempotency: INSERT OR IGNORE on UNIQUE(token_id, evicted_at). Re-running is a
no-op; and because the live writer stamps the SAME evicted_at on both the DB row
and the journal line, backfilling a live eviction's journal record dedups
against the live row rather than double-counting. Pre-marker journal lines lack
``evicted_at`` and fall back to the structlog ``timestamp`` (that gap period has
no live rows, so no collision).

Dry-run by DEFAULT: prints the matched row count without writing. Pass --apply
to insert.

Windows-safe: imports only scout.db (no aiohttp import chain). Usage:

    python -m scripts.backfill_ledger_eviction_markers \\
        --db /root/gecko-alpha/scout.db \\
        --journal /var/lib/gecko-alpha/ledger_eviction_export.jsonl
    # add --apply to write
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any, Iterator

from scout.db import Database

_DEFAULT_JOURNAL = "/var/lib/gecko-alpha/ledger_eviction_export.jsonl"
_EVENT = "ledger_enrollment_evicted"


def _structlog_obj(line: str) -> dict[str, Any] | None:
    """Extract the structlog dict from one export line.

    Handles both the journald envelope (``{"MESSAGE": "<structlog json>", ...}``)
    and a bare structlog JSON line. Returns None for blanks / unparseable lines
    / non-eviction events.
    """
    raw = line.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    # journald envelope: the structlog event is JSON inside .MESSAGE.
    if "MESSAGE" in obj and "event" not in obj:
        message = obj.get("MESSAGE")
        if isinstance(message, list):  # journald can split long lines into bytes
            try:
                message = bytes(message).decode("utf-8", "replace")
            except (TypeError, ValueError):
                return None
        if not isinstance(message, str):
            return None
        try:
            obj = json.loads(message)
        except json.JSONDecodeError:
            return None
        if not isinstance(obj, dict):
            return None
    if obj.get("event") != _EVENT:
        return None
    return obj


def iter_eviction_records(journal_path: Path) -> Iterator[dict[str, Any]]:
    """Yield one per-token marker dict for each evicted token across the file.

    Each dict has: token_id, evicted_at, evicted_for, max_active, n_evicted.
    ``evicted_at`` is the log's own evicted_at when present (the live-write key)
    else the structlog ``timestamp`` fallback for pre-marker lines.
    """
    for line in journal_path.read_text(encoding="utf-8").splitlines():
        obj = _structlog_obj(line)
        if obj is None:
            continue
        token_ids = obj.get("evicted_token_ids")
        if not isinstance(token_ids, list) or not token_ids:
            continue
        evicted_at = obj.get("evicted_at") or obj.get("timestamp")
        if not evicted_at:
            continue
        evicted_for = obj.get("evicted_for")
        max_active = obj.get("max_active")
        n_evicted = obj.get("n_evicted")
        for token_id in token_ids:
            if not token_id:
                continue
            yield {
                "token_id": str(token_id),
                "evicted_at": str(evicted_at),
                "evicted_for": str(evicted_for) if evicted_for else None,
                "max_active": max_active,
                "n_evicted": n_evicted,
            }


async def backfill_file(db_path: str | Path, journal_path: Path, *, apply: bool) -> int:
    """Backfill markers from *journal_path* into *db_path*.

    Dry-run (apply=False): returns the number of matched per-token records
    without writing. Apply: returns the number of rows ACTUALLY inserted (new
    (token_id, evicted_at) keys; existing rows are ignored — idempotent).
    """
    records = list(iter_eviction_records(journal_path))
    if not apply:
        return len(records)

    db = Database(str(db_path))
    await db.initialize()
    try:
        inserted = 0
        for rec in records:
            cur = await db._conn.execute(
                "INSERT OR IGNORE INTO ledger_enrollment_evictions "
                "(token_id, evicted_at, evicted_for, max_active, n_evicted, source) "
                "VALUES (?, ?, ?, ?, ?, 'journal_backfill')",
                (
                    rec["token_id"],
                    rec["evicted_at"],
                    rec["evicted_for"],
                    rec["max_active"],
                    rec["n_evicted"],
                ),
            )
            inserted += int(cur.rowcount or 0)
        await db._conn.commit()
        return inserted
    finally:
        await db.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill ledger_enrollment_evictions from the journal-export JSONL "
            "(scripts/ledger-eviction-export.sh). Dry-run by default."
        )
    )
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--journal", type=Path, default=Path(_DEFAULT_JOURNAL))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write rows. Omit for a dry-run (default) that only counts matches.",
    )
    return parser


async def _main_async() -> int:
    args = _build_parser().parse_args()
    count = await backfill_file(args.db, args.journal, apply=args.apply)
    action = "inserted" if args.apply else "matched"
    print(f"{action}={count} journal={args.journal}")
    return 0


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
