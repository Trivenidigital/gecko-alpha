#!/usr/bin/env python3
"""Re-runnable social_mentions_24h denominator evidence audit.

Read-only SQLite audit for the dead Signal 5 denominator question. It does not
change scoring behavior; it quantifies whether the prior Option B/C conclusions
still hold against the supplied DB.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scout.scorer import SCORER_MAX_RAW

SOCIAL_MENTIONS_POINTS = 15
SOCIAL_MENTIONS_THRESHOLD = 50
MIN_SCORE = 60
CONVICTION_THRESHOLD = 70
VARIANT_B_MIN_SCORE = 65
VARIANT_B_CONVICTION_THRESHOLD = 75

REQUIRED_COLUMNS = {
    "candidates": ("social_mentions_24h",),
    "score_history": ("contract_address", "score", "scanned_at"),
}

OPTIONAL_TABLES = (
    "paper_trades",
    "narrative_alerts_inbound",
    "tg_social_messages",
    "social_signals",
    "social_baselines",
    "social_credit_ledger",
)


class SchemaError(RuntimeError):
    pass


def _utc_iso_z(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_readonly(db_path: str) -> sqlite3.Connection:
    path = Path(db_path)
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _require_schema(conn: sqlite3.Connection) -> None:
    missing: list[str] = []
    for table, columns in REQUIRED_COLUMNS.items():
        if not _table_exists(conn, table):
            missing.append(f"{table} table")
            continue
        present = _columns(conn, table)
        for column in columns:
            if column not in present:
                missing.append(f"{table}.{column}")
    if missing:
        raise SchemaError("schema missing required fields: " + ", ".join(missing))


def _one(
    conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()
) -> dict[str, Any]:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else {}


def _optional_count(conn: sqlite3.Connection, table: str) -> int | None:
    if not _table_exists(conn, table):
        return None
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _social_mentions(conn: sqlite3.Connection) -> dict[str, Any]:
    row = _one(
        conn,
        f"""
        SELECT
          COUNT(*) AS total_candidates,
          SUM(CASE WHEN social_mentions_24h > ? THEN 1 ELSE 0 END) AS would_fire_signal_5,
          SUM(CASE WHEN social_mentions_24h > 0 THEN 1 ELSE 0 END) AS nonzero,
          MAX(social_mentions_24h) AS max_value
        FROM candidates
        """,
        (SOCIAL_MENTIONS_THRESHOLD,),
    )
    return {k: (0 if v is None and k != "max_value" else v) for k, v in row.items()}


def _score_history(conn: sqlite3.Connection) -> dict[str, Any]:
    row = _one(
        conn,
        """
        SELECT
          COUNT(*) AS rows,
          MAX(score) AS max_score,
          SUM(CASE WHEN score >= ? THEN 1 ELSE 0 END) AS gte_min_score,
          SUM(CASE WHEN score >= ? THEN 1 ELSE 0 END) AS gte_conviction,
          MIN(scanned_at) AS oldest_row,
          MAX(scanned_at) AS newest_row
        FROM score_history
        """,
        (MIN_SCORE, CONVICTION_THRESHOLD),
    )
    return {k: (0 if v is None and k.startswith("gte_") else v) for k, v in row.items()}


def _variant_b(conn: sqlite3.Connection) -> dict[str, Any]:
    without_social = SCORER_MAX_RAW - SOCIAL_MENTIONS_POINTS
    return _one(
        conn,
        """
        WITH recalc AS (
          SELECT score AS current_score,
                 MIN(100, CAST(score * ? / 100.0 / ? * 100 AS INTEGER)) AS new_score
          FROM score_history
        )
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN current_score >= ? AND new_score < ? THEN 1 ELSE 0 END) AS demoted_min_60_to_65,
          SUM(CASE WHEN current_score < ? AND new_score >= ? THEN 1 ELSE 0 END) AS promoted_min_60_to_65,
          SUM(CASE WHEN current_score >= ? AND new_score < ? THEN 1 ELSE 0 END) AS demoted_conviction_70_to_75,
          SUM(CASE WHEN current_score < ? AND new_score >= ? THEN 1 ELSE 0 END) AS promoted_conviction_70_to_75
        FROM recalc
        """,
        (
            float(SCORER_MAX_RAW),
            float(without_social),
            MIN_SCORE,
            VARIANT_B_MIN_SCORE,
            MIN_SCORE,
            VARIANT_B_MIN_SCORE,
            CONVICTION_THRESHOLD,
            VARIANT_B_CONVICTION_THRESHOLD,
            CONVICTION_THRESHOLD,
            VARIANT_B_CONVICTION_THRESHOLD,
        ),
    )


def _variant_c(conn: sqlite3.Connection) -> dict[str, Any]:
    without_social = SCORER_MAX_RAW - SOCIAL_MENTIONS_POINTS
    base = _one(
        conn,
        """
        WITH recalc AS (
          SELECT score AS current_score,
                 MIN(100, CAST(score * ? / 100.0 / ? * 100 AS INTEGER)) AS new_score
          FROM score_history
        )
        SELECT
          SUM(CASE WHEN current_score < ? AND new_score >= ? THEN 1 ELSE 0 END) AS newly_passes_min_60,
          SUM(CASE WHEN current_score < ? AND new_score >= ? THEN 1 ELSE 0 END) AS newly_passes_conviction_70
        FROM recalc
        """,
        (
            float(SCORER_MAX_RAW),
            float(without_social),
            MIN_SCORE,
            MIN_SCORE,
            CONVICTION_THRESHOLD,
            CONVICTION_THRESHOLD,
        ),
    )
    base = {k: (0 if v is None else v) for k, v in base.items()}
    base["rounded_sensitivity"] = _one(
        conn,
        """
        WITH recalc AS (
          SELECT score AS current_score,
                 MIN(100, CAST(score * ? / 100.0 / ? * 100 + 0.5 AS INTEGER)) AS new_score
          FROM score_history
        )
        SELECT
          SUM(CASE WHEN current_score < ? AND new_score >= ? THEN 1 ELSE 0 END) AS rounded_newly_passes_min_60,
          SUM(CASE WHEN current_score < ? AND new_score >= ? THEN 1 ELSE 0 END) AS rounded_newly_passes_conviction_70
        FROM recalc
        """,
        (
            float(SCORER_MAX_RAW),
            float(without_social),
            MIN_SCORE,
            MIN_SCORE,
            CONVICTION_THRESHOLD,
            CONVICTION_THRESHOLD,
        ),
    )
    base["paper_trade_cross_check"] = _paper_trade_cross_check(conn)
    return base


def _paper_trade_cross_check(conn: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(conn, "paper_trades"):
        return {"available": False, "reason": "paper_trades table missing"}
    without_social = SCORER_MAX_RAW - SOCIAL_MENTIONS_POINTS
    row = _one(
        conn,
        """
        WITH promoted AS (
          SELECT DISTINCT contract_address
          FROM score_history
          WHERE score < ?
            AND CAST(score * ? / 100.0 / ? * 100 AS INTEGER) >= ?
        )
        SELECT
          (SELECT COUNT(*) FROM promoted) AS n_promoted_candidates,
          COUNT(DISTINCT pt.token_id) AS n_with_paper_trades,
          ROUND(SUM(pt.pnl_usd), 2) AS total_pnl_usd,
          ROUND(
            100.0 * SUM(CASE WHEN pt.pnl_usd > 0 THEN 1 ELSE 0 END)
            / NULLIF(COUNT(pt.id), 0),
            1
          ) AS win_pct
        FROM paper_trades pt
        INNER JOIN promoted p ON pt.token_id = p.contract_address
        """,
        (MIN_SCORE, float(SCORER_MAX_RAW), float(without_social), MIN_SCORE),
    )
    row["available"] = True
    row["n_with_paper_trades"] = row.get("n_with_paper_trades") or 0
    return row


def _bridges(conn: sqlite3.Connection, now: datetime) -> dict[str, Any]:
    now_iso = now.astimezone(timezone.utc).isoformat()
    out: dict[str, Any] = {}
    if _table_exists(conn, "narrative_alerts_inbound"):
        out["narrative_alerts_inbound_7d"] = _one(
            conn,
            """
            SELECT
              COUNT(*) AS total,
              SUM(CASE WHEN resolved_coin_id IS NOT NULL AND resolved_coin_id != '' THEN 1 ELSE 0 END) AS resolved
            FROM narrative_alerts_inbound
            WHERE datetime(received_at) >= datetime(?, '-7 days')
            """,
            (now_iso,),
        )
    else:
        out["narrative_alerts_inbound_7d"] = {"available": False}

    if _table_exists(conn, "tg_social_messages"):
        out["tg_social_messages_24h"] = _one(
            conn,
            """
            WITH recent AS (
              SELECT contracts
              FROM tg_social_messages
              WHERE datetime(parsed_at) >= datetime(?, '-1 day')
                AND contracts IS NOT NULL AND contracts != '' AND contracts != '[]'
            ),
            exploded AS (
              SELECT lower(
                COALESCE(
                  CASE
                    WHEN json_valid(j.value) THEN json_extract(j.value, '$.address')
                    ELSE NULL
                  END,
                  j.value
                )
              ) AS contract
              FROM recent r
              JOIN json_each(
                CASE WHEN json_valid(r.contracts) THEN r.contracts ELSE '[]' END
              ) AS j
              WHERE COALESCE(
                  CASE
                    WHEN json_valid(j.value) THEN json_extract(j.value, '$.address')
                    ELSE NULL
                  END,
                  j.value
                ) IS NOT NULL
                AND COALESCE(
                  CASE
                    WHEN json_valid(j.value) THEN json_extract(j.value, '$.address')
                    ELSE NULL
                  END,
                  j.value
                ) != ''
            )
            SELECT
              (SELECT COUNT(DISTINCT contract) FROM exploded) AS distinct_contracts,
              (SELECT COUNT(*) FROM recent) AS total_msgs_with_contracts,
              (
                SELECT COUNT(*)
                FROM recent
                WHERE NOT json_valid(contracts)
              ) AS invalid_contract_json_rows
            """,
            (now_iso,),
        )
    else:
        out["tg_social_messages_24h"] = {"available": False}
    return out


def build_report(db_path: str, *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    conn = open_readonly(db_path)
    try:
        _require_schema(conn)
        return {
            "stage": "ok",
            "audited_at": _utc_iso_z(now),
            "params": {
                "scorer_max_raw": SCORER_MAX_RAW,
                "social_mentions_points": SOCIAL_MENTIONS_POINTS,
                "max_raw_without_social": SCORER_MAX_RAW - SOCIAL_MENTIONS_POINTS,
                "social_mentions_threshold": SOCIAL_MENTIONS_THRESHOLD,
                "current_min_score": MIN_SCORE,
                "variant_b_min_score": VARIANT_B_MIN_SCORE,
                "current_conviction_threshold": CONVICTION_THRESHOLD,
                "variant_b_conviction_threshold": VARIANT_B_CONVICTION_THRESHOLD,
                "closed_form_caveat": (
                    "score_history stores final scores only; raw points and signal "
                    "lists are not persisted, so Variant B/C are approximations."
                ),
            },
            "social_mentions": _social_mentions(conn),
            "score_history": _score_history(conn),
            "variant_b": _variant_b(conn),
            "variant_c": _variant_c(conn),
            "bridges": _bridges(conn, now),
            "optional_table_counts": {
                table: _optional_count(conn, table) for table in OPTIONAL_TABLES
            },
        }
    finally:
        conn.close()


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit social_mentions_24h scorer denominator evidence."
    )
    parser.add_argument("--db", default="scout.db", help="Path to SQLite DB")
    parser.add_argument("--output", help="Write JSON report to this path")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        report = build_report(args.db)
    except (sqlite3.Error, SchemaError) as exc:
        print(json.dumps({"stage": "schema", "error": str(exc)}), file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2 if args.pretty else None, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
