#!/usr/bin/env python3
"""Liquidity coverage audit for Today's Focus.

Read-only diagnostic. Consumes the live ``/api/todays_focus`` endpoint output
(so the cohort matches what the trader sees) and attempts a candidates-table
liquidity lookup for each paper-corpus row. Reports joinable vs unjoinable
counts as first-class fields so a low coverage rate is not silently
attributed to "missing liquidity" when the truth is "unjoinable key space."

The script never writes to the database (opened in URI read-only mode) and
never writes to disk except stdout. Coverage thresholds (e.g., 80% for PR-B
shipping decision) live in PR-B's plan, NOT in this script.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any


TRACKER_STRUCTURAL_NOTE = (
    "No CG-coin_id-keyed table has a liquidity column; tracker liquidity "
    "is a backfill gap."
)

SCHEMA_TABLES_WITH_LIQUIDITY_CHECK = (
    ("candidates", "liquidity_usd", "candidates_has_liquidity_usd"),
    ("gainers_comparisons", "liquidity_usd", "gainers_comparisons_has_liquidity"),
    ("price_cache", "liquidity_usd", "price_cache_has_liquidity"),
    ("volume_history_cg", "liquidity_usd", "volume_history_cg_has_liquidity"),
    ("trending_comparisons", "liquidity_usd", "trending_comparisons_has_liquidity"),
)


def _utc_iso_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_focus_rows(url: str, window_hours: int, timeout: float) -> tuple[str, list[dict]]:
    full_url = f"{url.rstrip('/')}/api/todays_focus?window_hours={window_hours}"
    req = urllib.request.Request(full_url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        rows = []
    return full_url, rows


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        cursor = conn.execute(f"PRAGMA table_info({table})")
    except sqlite3.Error:
        return False
    return any(row[1] == column for row in cursor.fetchall())


def _schema_findings(conn: sqlite3.Connection) -> dict[str, bool]:
    return {
        flag: _column_exists(conn, table, column)
        for table, column, flag in SCHEMA_TABLES_WITH_LIQUIDITY_CHECK
    }


def _lookup_candidate_liquidity(
    conn: sqlite3.Connection, token_id: str
) -> tuple[bool, float | None]:
    """Return (joinable, liquidity_usd) for a paper-corpus token_id.

    Tries exact match first, then case-insensitive. Returns (False, None) if
    no candidates row matches either lookup.
    """
    if not token_id:
        return False, None
    try:
        cursor = conn.execute(
            "SELECT liquidity_usd FROM candidates WHERE contract_address = ? LIMIT 1",
            (token_id,),
        )
        row = cursor.fetchone()
        if row is None:
            cursor = conn.execute(
                "SELECT liquidity_usd FROM candidates "
                "WHERE LOWER(contract_address) = LOWER(?) LIMIT 1",
                (token_id,),
            )
            row = cursor.fetchone()
    except sqlite3.Error:
        return False, None
    if row is None:
        return False, None
    liquidity = row[0]
    if liquidity is None:
        return True, None
    try:
        return True, float(liquidity)
    except (TypeError, ValueError):
        return True, None


def _is_valid_liquidity(value: float | None) -> bool:
    return value is not None and value > 0


def _rate_or_null(num: int, denom: int) -> float | None:
    if denom <= 0:
        return None
    return round(num / denom, 4)


def _classify_paper_rows(
    paper_rows: list[dict], conn: sqlite3.Connection
) -> dict[str, Any]:
    joinable = 0
    with_liquidity = 0
    by_chain: dict[str, dict[str, int]] = {}

    for row in paper_rows:
        chain = row.get("chain") or "<empty>"
        chain_bucket = by_chain.setdefault(
            chain, {"rows": 0, "joinable": 0, "with_liquidity": 0}
        )
        chain_bucket["rows"] += 1

        token_id = row.get("token_id") or ""
        is_joinable, liquidity = _lookup_candidate_liquidity(conn, token_id)
        if is_joinable:
            joinable += 1
            chain_bucket["joinable"] += 1
            if _is_valid_liquidity(liquidity):
                with_liquidity += 1
                chain_bucket["with_liquidity"] += 1

    by_chain_out = {}
    for chain, bucket in by_chain.items():
        by_chain_out[chain] = {
            **bucket,
            "coverage_rate": _rate_or_null(bucket["with_liquidity"], bucket["rows"]),
        }

    rows_count = len(paper_rows)
    return {
        "rows": rows_count,
        "joinable_to_candidates": joinable,
        "unjoinable_to_candidates": rows_count - joinable,
        "join_rate": _rate_or_null(joinable, rows_count),
        "rows_with_valid_liquidity": with_liquidity,
        "coverage_rate": _rate_or_null(with_liquidity, rows_count),
        "by_chain": by_chain_out,
    }


def _classify_tracker_rows(tracker_rows: list[dict]) -> dict[str, Any]:
    return {
        "rows": len(tracker_rows),
        "rows_with_liquidity_source": 0,
        "structural_note": TRACKER_STRUCTURAL_NOTE,
    }


def build_report(
    endpoint_url: str,
    rows: list[dict],
    conn: sqlite3.Connection,
    window_hours: int,
) -> dict[str, Any]:
    paper_rows = [r for r in rows if r.get("source_corpus") == "paper"]
    tracker_rows = [r for r in rows if r.get("source_corpus") == "tracker"]
    return {
        "audited_at": _utc_iso_z(),
        "window_hours": window_hours,
        "endpoint_url": endpoint_url,
        "total_rows": len(rows),
        "paper_corpus": _classify_paper_rows(paper_rows, conn),
        "tracker_corpus": _classify_tracker_rows(tracker_rows),
        "schema_findings": _schema_findings(conn),
    }


def _format_human(report: dict[str, Any]) -> str:
    lines = []
    lines.append(f"audited_at: {report['audited_at']}")
    lines.append(
        f"endpoint:   {report['endpoint_url']} (window={report['window_hours']}h)"
    )
    lines.append(f"total_rows: {report['total_rows']}")
    paper = report["paper_corpus"]
    lines.append("")
    lines.append("PAPER CORPUS:")
    lines.append(f"  rows                       = {paper['rows']}")
    lines.append(f"  joinable_to_candidates     = {paper['joinable_to_candidates']}")
    lines.append(f"  unjoinable_to_candidates   = {paper['unjoinable_to_candidates']}")
    lines.append(f"  join_rate                  = {paper['join_rate']}")
    lines.append(f"  rows_with_valid_liquidity  = {paper['rows_with_valid_liquidity']}")
    lines.append(f"  coverage_rate              = {paper['coverage_rate']}")
    if paper["by_chain"]:
        lines.append("  by_chain:")
        for chain in sorted(paper["by_chain"].keys()):
            bucket = paper["by_chain"][chain]
            lines.append(
                f"    {chain!r}: rows={bucket['rows']}, "
                f"joinable={bucket['joinable']}, "
                f"with_liquidity={bucket['with_liquidity']}, "
                f"rate={bucket['coverage_rate']}"
            )
    tracker = report["tracker_corpus"]
    lines.append("")
    lines.append("TRACKER CORPUS:")
    lines.append(f"  rows                       = {tracker['rows']}")
    lines.append(
        f"  rows_with_liquidity_source = {tracker['rows_with_liquidity_source']}"
    )
    lines.append(f"  structural_note            = {tracker['structural_note']}")
    lines.append("")
    lines.append("SCHEMA FINDINGS (from PRAGMA table_info):")
    for flag, present in report["schema_findings"].items():
        lines.append(f"  {flag} = {present}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:8000",
        help="Dashboard base URL (default: %(default)s)",
    )
    parser.add_argument(
        "--db",
        default="scout.db",
        help="Path to SQLite DB (opened read-only; default: %(default)s)",
    )
    parser.add_argument(
        "--window-hours",
        type=int,
        default=36,
        help="Today's Focus window in hours (default: %(default)s)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="HTTP timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON to stdout instead of human-readable summary",
    )
    args = parser.parse_args()

    try:
        endpoint_url, rows = _fetch_focus_rows(args.url, args.window_hours, args.timeout)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        msg = {"status": "error", "stage": "fetch", "error": str(exc)}
        if args.json:
            print(json.dumps(msg))
        else:
            print(f"ERROR: cannot fetch /api/todays_focus: {exc}", file=sys.stderr)
        return 2

    try:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        msg = {"status": "error", "stage": "db_open", "error": str(exc)}
        if args.json:
            print(json.dumps(msg))
        else:
            print(f"ERROR: cannot open DB read-only: {exc}", file=sys.stderr)
        return 2

    try:
        report = build_report(endpoint_url, rows, conn, args.window_hours)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report))
    else:
        print(_format_human(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
