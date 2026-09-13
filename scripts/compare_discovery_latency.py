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
from collections import Counter

# Reuse the ingestion adapter's explicit aliases; do not infer CG slug identity.
# Keep direct script invocation working without requiring an installed package.
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scout.ingestion.geckoterminal import GECKOTERMINAL_NETWORK_BY_CHAIN


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        # Normalize the 'T' vs space divergence the same way the PR #24 audit
        # settled it for in-pipeline SQL: accept both. Naive values are UTC
        # by repo convention (SQL datetime() strips the +00:00 offset).
        parsed = datetime.fromisoformat(value.replace(" ", "T"))
    except (ValueError, TypeError, AttributeError):
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


def _network(value: str) -> str:
    value = (value or "").lower()
    return GECKOTERMINAL_NETWORK_BY_CHAIN.get(value, value)


def _earliest_observation(conn, table, address_column, chain_column, token, network):
    """Select absolute instants in Python: SQLite datetime truncates fractions.

    Table/column arguments are internal constants. Unknown chain aliases stay
    unmatched; a same-address observation on another network is not evidence.
    """
    matching = [
        row["first_seen_at"]
        for row in conn.execute(
            f"SELECT first_seen_at, {chain_column} AS network FROM {table} "
            f"WHERE LOWER({address_column}) = ?",
            (token,),
        )
        if _network(row["network"]) == _network(network)
    ]
    valid = [
        (parsed, raw) for raw in matching if (parsed := _parse_ts(raw)) is not None
    ]
    invalid_count = len(matching) - len(valid)
    # A malformed clock may belong to an earlier observation. Selecting the
    # earliest parseable row would overstate latency and manufacture RH lift.
    return (
        (
            min(valid, key=lambda item: item[0])[1]
            if valid and not invalid_count
            else None
        ),
        len(matching),
        invalid_count,
    )


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

    paired = {"cg_ds_gt": [], "dex_lane": []}
    invalid_clock_counts = Counter()

    for launch in launches:
        token = (launch["token_address"] or "").lower()
        event_time = _parse_ts(launch["event_time"])
        rh_seen = _parse_ts(launch["first_seen_at"])

        cg_seen_raw, cg_matches, cg_invalid = None, 0, 0
        if have_candidates:
            cg_seen_raw, cg_matches, cg_invalid = _earliest_observation(
                conn,
                "candidates",
                "contract_address",
                "chain",
                token,
                launch["network"],
            )
        dex_seen_raw, dex_matches, dex_invalid = None, 0, 0
        if have_dex:
            dex_seen_raw, dex_matches, dex_invalid = _earliest_observation(
                conn,
                "dex_pool_discoveries",
                "base_token_address",
                "network",
                token,
                launch["network"],
            )

        provider_available_at = "unknown"
        if have_events:
            provider_rows = conn.execute(
                "SELECT provider_available_at FROM curve_launch_events "
                "WHERE chain_id = ? AND LOWER(token_address) = ? "
                "AND provider_available_at IS NOT NULL",
                (launch["chain_id"], token),
            ).fetchall()
            valid_provider = [
                (parsed, row[0])
                for row in provider_rows
                if (parsed := _parse_ts(row[0])) is not None
            ]
            if valid_provider:
                provider_available_at = min(valid_provider, key=lambda item: item[0])[1]

        cg_seen = _parse_ts(cg_seen_raw)
        dex_seen = _parse_ts(dex_seen_raw)

        censored: list[str] = []
        if event_time is None:
            censored.append("event_time_unavailable")
            censor_counts["event_time_unavailable"] += 1
        if not cg_matches:
            censored.append("never_observed_cg_ds_gt")
            censor_counts["never_observed_cg_ds_gt"] += 1
        if not dex_matches:
            censored.append("never_observed_dex_lane")
            censor_counts["never_observed_dex_lane"] += 1

        event_to_rh = _delta_seconds(event_time, rh_seen)
        event_to_cg = _delta_seconds(event_time, cg_seen)
        event_to_dex = _delta_seconds(event_time, dex_seen)
        for label, count in (("cg_ds_gt", cg_invalid), ("dex_lane", dex_invalid)):
            if count:
                reason = f"invalid_{label}_clock"
                censored.append(reason)
                invalid_clock_counts[reason] += count
        if rh_seen is None:
            censored.append("invalid_rh_clock")
            invalid_clock_counts["invalid_rh_clock"] += 1
        if launch["event_time"] and event_time is None:
            censored.append("invalid_event_clock")
            invalid_clock_counts["invalid_event_clock"] += 1
        for label, delta, values in (
            ("rh", event_to_rh, rh_latencies),
            ("cg_ds_gt", event_to_cg, cg_latencies),
            ("dex_lane", event_to_dex, dex_latencies),
        ):
            if delta is not None and delta < 0:
                reason = f"negative_event_to_{label}"
                censored.append(reason)
                invalid_clock_counts[reason] += 1
            elif delta is not None:
                values.append(delta)
        advantages = {}
        for label, delta in (("cg_ds_gt", event_to_cg), ("dex_lane", event_to_dex)):
            # Pair only clocks for the same launch, with valid nonnegative
            # event-relative measurements. Positive means RH observed earlier.
            advantage = (
                delta - event_to_rh
                if delta is not None
                and delta >= 0
                and event_to_rh is not None
                and event_to_rh >= 0
                else None
            )
            advantages[f"rh_advantage_vs_{label}_seconds"] = advantage
            if advantage is not None:
                paired[label].append(advantage)

        rows.append(
            {
                "chain_id": launch["chain_id"],
                **advantages,
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
        "invalid_clock_counts": dict(invalid_clock_counts),
        "provenance_counts": dict(Counter(row["provenance"] for row in rows)),
        "measurement_scope": "Local observation clocks; provider availability is unknown unless explicitly recorded. Fixture provenance is not live effectiveness evidence.",
        "paired_advantage_summary": {
            f"rh_vs_{label}": {
                **_summary(values),
                "censored_n": len(rows) - len(values),
                "rh_earlier_n": sum(value > 0 for value in values),
                "rh_later_n": sum(value < 0 for value in values),
                "tied_n": sum(value == 0 for value in values),
            }
            for label, values in paired.items()
        },
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
