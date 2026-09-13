#!/usr/bin/env python3
"""Discovery-latency comparison harness (read-only).

For every curve-native launch in `curve_launch_discoveries`, measures and
compares three distinct clocks (design_rh_pons_discovery_delta_2026_09_13):

  event_to_rh_seconds      onchain event time -> RH collector first_seen_at
  event_to_cg_ds_gt_seconds onchain event time -> candidates.first_seen_at
                            (the CG/DS/GT ingestion lanes' earliest sighting)
  event_to_dex_lane_seconds onchain event time -> dex_pool_discoveries
                            first sighting (GT new-pools research lane)

Censoring is explicit and counted, never silently zero:
  event_time_unavailable    the launch has no verified block timestamp
  never_observed_cg_ds_gt   no candidates row matches the contract
  never_observed_dex_lane   no dex_pool_discoveries row matches the contract

Provider availability time is reported per launch when any evidence row
carries `provider_available_at`; otherwise "unknown" — a historical block
timestamp does not prove a provider could have served the fact then.

Output: one structured JSON document on stdout. Never writes to the DB
(opened with mode=ro).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import datetime, timezone


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Normalize the 'T' vs space divergence the same way the PR #24 audit
        # settled it for in-pipeline SQL: accept both. Naive values are UTC
        # by repo convention (SQL datetime() strips the +00:00 offset).
        parsed = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _delta_seconds(start: datetime | None, end: datetime | None) -> float | None:
    if start is None or end is None:
        return None
    return (end - start).total_seconds()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def compare(conn: sqlite3.Connection) -> dict:
    conn.row_factory = sqlite3.Row
    if not _table_exists(conn, "curve_launch_discoveries"):
        return {
            "error": "curve_launch_discoveries table absent (migration not applied)",
            "launches": [],
        }

    launches = conn.execute(
        "SELECT chain_id, network, protocol, token_address, lifecycle_status,"
        "       event_time, first_seen_at, provenance "
        "FROM curve_launch_discoveries ORDER BY first_seen_at"
    ).fetchall()

    have_candidates = _table_exists(conn, "candidates")
    have_dex = _table_exists(conn, "dex_pool_discoveries")
    have_events = _table_exists(conn, "curve_launch_events")

    rows: list[dict] = []
    censor_counts = {
        "event_time_unavailable": 0,
        "never_observed_cg_ds_gt": 0,
        "never_observed_dex_lane": 0,
    }
    rh_latencies: list[float] = []
    cg_latencies: list[float] = []
    dex_latencies: list[float] = []

    for launch in launches:
        token = (launch["token_address"] or "").lower()
        event_time = _parse_ts(launch["event_time"])
        rh_seen = _parse_ts(launch["first_seen_at"])

        cg_seen_raw = None
        if have_candidates:
            row = conn.execute(
                "SELECT MIN(datetime(first_seen_at)) AS fs FROM candidates "
                "WHERE LOWER(contract_address) = ?",
                (token,),
            ).fetchone()
            cg_seen_raw = row["fs"] if row else None
        dex_seen_raw = None
        if have_dex:
            row = conn.execute(
                "SELECT MIN(datetime(first_seen_at)) AS fs FROM dex_pool_discoveries "
                "WHERE LOWER(base_token_address) = ?",
                (token,),
            ).fetchone()
            dex_seen_raw = row["fs"] if row else None

        provider_available_at = "unknown"
        if have_events:
            row = conn.execute(
                "SELECT MIN(provider_available_at) AS pa FROM curve_launch_events "
                "WHERE chain_id = ? AND token_address = ? "
                "AND provider_available_at IS NOT NULL",
                (launch["chain_id"], token),
            ).fetchone()
            if row and row["pa"]:
                provider_available_at = row["pa"]

        cg_seen = _parse_ts(cg_seen_raw)
        dex_seen = _parse_ts(dex_seen_raw)

        censored: list[str] = []
        if event_time is None:
            censored.append("event_time_unavailable")
            censor_counts["event_time_unavailable"] += 1
        if cg_seen is None:
            censored.append("never_observed_cg_ds_gt")
            censor_counts["never_observed_cg_ds_gt"] += 1
        if dex_seen is None:
            censored.append("never_observed_dex_lane")
            censor_counts["never_observed_dex_lane"] += 1

        event_to_rh = _delta_seconds(event_time, rh_seen)
        event_to_cg = _delta_seconds(event_time, cg_seen)
        event_to_dex = _delta_seconds(event_time, dex_seen)
        if event_to_rh is not None:
            rh_latencies.append(event_to_rh)
        if event_to_cg is not None:
            cg_latencies.append(event_to_cg)
        if event_to_dex is not None:
            dex_latencies.append(event_to_dex)

        rows.append(
            {
                "token_address": token,
                "network": launch["network"],
                "protocol": launch["protocol"],
                "lifecycle_status": launch["lifecycle_status"],
                "provenance": launch["provenance"],
                "event_time": launch["event_time"],
                "rh_first_seen_at": launch["first_seen_at"],
                "cg_ds_gt_first_seen_at": cg_seen_raw,
                "dex_lane_first_seen_at": dex_seen_raw,
                "provider_available_at": provider_available_at,
                "event_to_rh_seconds": event_to_rh,
                "event_to_cg_ds_gt_seconds": event_to_cg,
                "event_to_dex_lane_seconds": event_to_dex,
                "censored": censored,
            }
        )

    def _summary(values: list[float]) -> dict:
        if not values:
            return {"n": 0, "median_seconds": None, "min_seconds": None}
        return {
            "n": len(values),
            "median_seconds": statistics.median(values),
            "min_seconds": min(values),
        }

    return {
        "launch_count": len(rows),
        "censoring": censor_counts,
        "latency_summary": {
            "event_to_rh": _summary(rh_latencies),
            "event_to_cg_ds_gt": _summary(cg_latencies),
            "event_to_dex_lane": _summary(dex_latencies),
        },
        "launches": rows,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="scout.db", help="Path to scout DB")
    args = parser.parse_args()
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        report = compare(conn)
    finally:
        conn.close()
    json.dump(report, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
