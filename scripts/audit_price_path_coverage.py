#!/usr/bin/env python3
"""Price-path coverage audit for Today's Focus sparkline gating.

Read-only diagnostic. Consumes the live ``/api/todays_focus`` endpoint (so
the cohort matches what the trader sees) and per-row counts ``price``
points in ``volume_history_cg`` within the lookback window. Reports
joinable-vs-unjoinable counts as first-class fields so a low coverage rate
is not silently attributed to "missing intraday data" when the truth is
"unjoinable key space."

Source-of-truth scope: ``volume_history_cg`` ONLY. This is the
markets-watcher cadence source intended to feed PR-C's sparkline rendering
(``scout/spikes/detector.py:18-58`` writes (coin_id, price, recorded_at)).
The writer prunes rows older than 7 days, so ``--lookback-hours`` is capped
at 168. Other price+timestamp tables (gainers_snapshots, losers_snapshots,
momentum_7d, slow_burn_candidates, volume_spikes) are documented but NOT
counted; PR-C decides whether to widen the data source.

Coverage thresholds (e.g., ">=12 points / >=80% of cohort" sparkline gate)
live in PR-C's plan, NOT in this script.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any


LOOKBACK_HOURS_CEILING = 168  # 7d writer retention; rows older are pruned.

ALTERNATE_PRICE_HISTORY_TABLES = (
    "gainers_snapshots",
    "losers_snapshots",
    "momentum_7d",
    "slow_burn_candidates",
    "volume_spikes",
)

INFINITY_GUARD_MAX = 1e308  # defensive ceiling against +Inf in REAL columns.


def _utc_iso_z(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cutoff_iso(now: datetime, lookback_hours: int) -> str:
    return (now - timedelta(hours=lookback_hours)).isoformat()


def _fetch_focus_rows(url: str, window_hours: int, timeout: float) -> tuple[str, list[dict]]:
    full_url = f"{url.rstrip('/')}/api/todays_focus?window_hours={window_hours}"
    req = urllib.request.Request(full_url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        rows = []
    return full_url, rows


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        cursor = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
            (table,),
        )
    except sqlite3.Error:
        return False
    return cursor.fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        cursor = conn.execute(f"PRAGMA table_info({table})")
    except sqlite3.Error:
        return False
    return any(row[1] == column for row in cursor.fetchall())


def _count_price_points(
    conn: sqlite3.Connection, coin_id: str, cutoff_iso: str
) -> int:
    if not coin_id:
        return 0
    try:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM volume_history_cg "
            "WHERE coin_id = ? AND recorded_at >= ? "
            "AND price IS NOT NULL AND price > 0 AND price < ?",
            (coin_id, cutoff_iso, INFINITY_GUARD_MAX),
        )
    except sqlite3.Error:
        return 0
    row = cursor.fetchone()
    if not row:
        return 0
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return 0


def _quantile(sorted_values: list[int], q: float) -> int:
    if not sorted_values:
        return 0
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    idx = max(0, min(n - 1, int(round(q * (n - 1)))))
    return sorted_values[idx]


def _points_distribution(points: list[int]) -> dict[str, int] | None:
    if len(points) < 5:
        return None
    s = sorted(points)
    return {
        "min": s[0],
        "p25": _quantile(s, 0.25),
        "median": _quantile(s, 0.50),
        "p75": _quantile(s, 0.75),
        "max": s[-1],
    }


def _rate_or_null(num: int, denom: int) -> float | None:
    if denom <= 0:
        return None
    return round(num / denom, 4)


def _classify_paper_rows(
    paper_rows: list[dict], conn: sqlite3.Connection, cutoff_iso: str
) -> dict[str, Any]:
    per_row = []
    joinable = 0
    points = []
    for row in paper_rows:
        token_id = row.get("token_id") or ""
        symbol = row.get("symbol") or ""
        count = _count_price_points(conn, token_id, cutoff_iso)
        if count > 0:
            joinable += 1
        points.append(count)
        per_row.append({"token_id": token_id, "symbol": symbol, "points": count})
    rows_count = len(paper_rows)
    return {
        "rows": rows_count,
        "joinable_by_token_id": joinable,
        "unjoinable_or_zero_points": rows_count - joinable,
        "join_rate": _rate_or_null(joinable, rows_count),
        "points_distribution": _points_distribution(points),
        "per_row": per_row,
    }


def _classify_tracker_rows(
    tracker_rows: list[dict], conn: sqlite3.Connection, cutoff_iso: str
) -> dict[str, Any]:
    per_row = []
    with_points = 0
    points = []
    for row in tracker_rows:
        coin_id = row.get("token_id") or ""  # tracker token_id IS the coin_id
        symbol = row.get("symbol") or ""
        count = _count_price_points(conn, coin_id, cutoff_iso)
        if count > 0:
            with_points += 1
        points.append(count)
        per_row.append({"token_id": coin_id, "symbol": symbol, "points": count})
    rows_count = len(tracker_rows)
    return {
        "rows": rows_count,
        "rows_with_at_least_one_point": with_points,
        "rows_with_zero_points": rows_count - with_points,
        "join_rate": _rate_or_null(with_points, rows_count),
        "points_distribution": _points_distribution(points),
        "per_row": per_row,
    }


def _schema_findings(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "volume_history_cg_has_price": _column_exists(conn, "volume_history_cg", "price"),
        "volume_history_cg_has_recorded_at": _column_exists(
            conn, "volume_history_cg", "recorded_at"
        ),
        "price_cache_has_history_table": _table_exists(conn, "price_cache_history"),
        "alternate_price_history_tables_present": {
            name: _table_exists(conn, name) for name in ALTERNATE_PRICE_HISTORY_TABLES
        },
    }


def build_report(
    endpoint_url: str,
    rows: list[dict],
    conn: sqlite3.Connection,
    window_hours: int,
    lookback_hours: int,
    now: datetime,
) -> dict[str, Any]:
    cutoff_iso = _cutoff_iso(now, lookback_hours)
    paper_rows = [r for r in rows if r.get("source_corpus") == "paper"]
    tracker_rows = [r for r in rows if r.get("source_corpus") == "tracker"]
    return {
        "audited_at": _utc_iso_z(now),
        "window_hours": window_hours,
        "lookback_hours": lookback_hours,
        "cutoff_iso": cutoff_iso,
        "endpoint_url": endpoint_url,
        "total_rows": len(rows),
        "paper_corpus": _classify_paper_rows(paper_rows, conn, cutoff_iso),
        "tracker_corpus": _classify_tracker_rows(tracker_rows, conn, cutoff_iso),
        "schema_findings": _schema_findings(conn),
    }


def _format_human(report: dict[str, Any]) -> str:
    lines = [
        f"audited_at:     {report['audited_at']}",
        f"endpoint:       {report['endpoint_url']}",
        f"window_hours:   {report['window_hours']}",
        f"lookback_hours: {report['lookback_hours']}",
        f"cutoff_iso:     {report['cutoff_iso']}",
        f"total_rows:     {report['total_rows']}",
        "",
        "PAPER CORPUS:",
    ]
    paper = report["paper_corpus"]
    lines.extend(
        [
            f"  rows                       = {paper['rows']}",
            f"  joinable_by_token_id       = {paper['joinable_by_token_id']}",
            f"  unjoinable_or_zero_points  = {paper['unjoinable_or_zero_points']}",
            f"  join_rate                  = {paper['join_rate']}",
            f"  points_distribution        = {paper['points_distribution']}",
        ]
    )
    for entry in paper["per_row"]:
        lines.append(
            f"    {entry['symbol']!r} token_id={entry['token_id']!r} points={entry['points']}"
        )
    tracker = report["tracker_corpus"]
    lines.extend(
        [
            "",
            "TRACKER CORPUS:",
            f"  rows                       = {tracker['rows']}",
            f"  rows_with_at_least_one_point = {tracker['rows_with_at_least_one_point']}",
            f"  rows_with_zero_points      = {tracker['rows_with_zero_points']}",
            f"  join_rate                  = {tracker['join_rate']}",
            f"  points_distribution        = {tracker['points_distribution']}",
        ]
    )
    for entry in tracker["per_row"]:
        lines.append(
            f"    {entry['symbol']!r} coin_id={entry['token_id']!r} points={entry['points']}"
        )
    lines.append("")
    lines.append("SCHEMA FINDINGS:")
    for key, value in report["schema_findings"].items():
        lines.append(f"  {key} = {value}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--db", default="scout.db")
    parser.add_argument("--window-hours", type=int, default=36)
    parser.add_argument(
        "--lookback-hours",
        type=int,
        default=24,
        help=(
            "Price-point lookback window. Must be 1..%(const)d (7-day writer "
            "retention ceiling)."
        ),
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.lookback_hours < 1 or args.lookback_hours > LOOKBACK_HOURS_CEILING:
        msg = {
            "status": "error",
            "stage": "args",
            "error": (
                f"--lookback-hours must be in [1, {LOOKBACK_HOURS_CEILING}] "
                "(7-day writer retention ceiling)."
            ),
        }
        if args.json:
            print(json.dumps(msg))
        else:
            print(msg["error"], file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)

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
        report = build_report(
            endpoint_url, rows, conn, args.window_hours, args.lookback_hours, now
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report))
    else:
        print(_format_human(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
