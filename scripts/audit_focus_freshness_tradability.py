"""Read-only audit: Today's Focus freshness / tradability gate diagnostics.

OFFLINE / DIAGNOSTIC ONLY. This script makes a single HTTP GET to the existing
`/api/todays_focus?window_hours=N` endpoint (the same cohort the trader sees)
and reports, per candidate FACTUAL gate, how many rows it WOULD exclude over the
full cohort and how many of the current top-N (rows[:N] AS ALREADY ORDERED — no
re-ranking) it would remove. It is a pure read-only counter.

It performs NO writes, opens NO database (it is endpoint-only — there is no
`conn`/sqlite involvement at all, because the cohort under audit is exactly the
endpoint's served rows, not the raw DB rows; auditing the DB would measure a
different population than the trader's view), adds NO endpoint, defines no route,
imports no curation module, and changes NO ranking/curation. Acting on these
numbers is a separate, out-of-scope, pipeline-affecting step.

Gates (EVALUABLE — backing field present on the focus row):
    stale_price               price_staleness_minutes (primary) / price_is_stale (bool fallback)
    missing_detected_price    current_move_pct is None (proxy; no entry_price on surface)
    too_old_since_detection   opened_age_hours > max_age_hours
    far_moved_from_detection  abs(current_move_pct) > max_move_pct

Gates (NOT-EVALUABLE — backing field ABSENT from the focus row today):
    no_venue_route            chart_url    (absent -> reported, never silently passed/failed)
    liquidity_unavailable     liquidity_usd (absent -> reported, never silently passed/failed)

Usage:
    python scripts/audit_focus_freshness_tradability.py --url http://127.0.0.1:8000 \
        --window-hours 36 [--stale-hours 24] [--max-age-hours 12] \
        [--max-move-pct 150] [--top-n 5] [--json]

Exit codes:
    0  success (report printed)
    2  bad CLI args, or fetch/transport/JSON-decode failure
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

ENDPOINT_PATH = "/api/todays_focus"
TOP_N = 5

# Sentinel distinguishing "key absent" from "key present, value None".
_MISSING = object()

# EVALUABLE gates read fields that exist on the focus row today.
EVALUABLE_GATES = (
    "stale_price",
    "missing_detected_price",
    "too_old_since_detection",
    "far_moved_from_detection",
)
# NOT-EVALUABLE gates back onto fields absent from the focus surface today.
NOT_EVALUABLE_GATES = (
    "no_venue_route",
    "liquidity_unavailable",
)
ALL_GATES = EVALUABLE_GATES + NOT_EVALUABLE_GATES

# Field checked per gate (for field_findings / schema_findings reporting).
GATE_FIELD = {
    "stale_price": "price_staleness_minutes|price_is_stale",
    "missing_detected_price": "current_move_pct",
    "too_old_since_detection": "opened_age_hours",
    "far_moved_from_detection": "current_move_pct",
    "no_venue_route": "chart_url",
    "liquidity_unavailable": "liquidity_usd",
}


def _rate_or_null(numerator: int, denominator: int) -> Optional[float]:
    """Return numerator/denominator, or None when denominator is zero."""
    if denominator <= 0:
        return None
    return numerator / denominator


def _fetch_focus_rows(endpoint_url: str, timeout: float) -> list[dict]:
    """GET the endpoint and return the rows list (raises on any failure)."""
    req = urllib.request.Request(endpoint_url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("rows", [])
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("endpoint did not return a rows list")
    return rows


# --------------------------------------------------------------------------- #
# Gate predicates: excluded(row) -> True | False | None                       #
#   True  = row WOULD be excluded by this gate                                 #
#   False = row survives this gate                                             #
#   None  = required field absent -> not evaluable (counted in field_findings) #
# --------------------------------------------------------------------------- #


def _gate_stale_price(row: dict, *, stale_hours: float) -> Optional[bool]:
    minutes = row.get("price_staleness_minutes", _MISSING)
    if minutes is _MISSING:
        # Fall back to the server's boolean staleness flag if present.
        flag = row.get("price_is_stale", _MISSING)
        if flag is _MISSING or flag is None:
            return None
        return bool(flag)
    if minutes is None:
        flag = row.get("price_is_stale", _MISSING)
        if flag is _MISSING or flag is None:
            return None
        return bool(flag)
    return minutes >= stale_hours * 60.0


def _gate_missing_detected_price(row: dict) -> Optional[bool]:
    move = row.get("current_move_pct", _MISSING)
    if move is _MISSING:
        return None
    # current_move_pct is None EXACTLY when entry/current price was missing.
    return move is None


def _gate_too_old_since_detection(row: dict, *, max_age_hours: float) -> Optional[bool]:
    age = row.get("opened_age_hours", _MISSING)
    if age is _MISSING or age is None:
        return None
    return age > max_age_hours


def _gate_far_moved_from_detection(row: dict, *, max_move_pct: float) -> Optional[bool]:
    move = row.get("current_move_pct", _MISSING)
    if move is _MISSING or move is None:
        return None
    return abs(move) > max_move_pct


def _gate_no_venue_route(row: dict) -> Optional[bool]:
    # chart_url is NOT emitted on the focus row -> not-evaluable on every row.
    val = row.get("chart_url", _MISSING)
    if val is _MISSING:
        return None
    return val is None or val == ""


def _gate_liquidity_unavailable(row: dict) -> Optional[bool]:
    # liquidity_usd is NOT on the focus row -> not-evaluable on every row.
    val = row.get("liquidity_usd", _MISSING)
    if val is _MISSING:
        return None
    # "unavailable" == datum missing, NOT a threshold on amount (that would be
    # curation / ranking -> out of scope).
    return val is None


def _evaluate_row(
    row: dict,
    *,
    stale_hours: float,
    max_age_hours: float,
    max_move_pct: float,
) -> dict[str, Optional[bool]]:
    """Return {gate_name: True|False|None} for one row."""
    return {
        "stale_price": _gate_stale_price(row, stale_hours=stale_hours),
        "missing_detected_price": _gate_missing_detected_price(row),
        "too_old_since_detection": _gate_too_old_since_detection(
            row, max_age_hours=max_age_hours
        ),
        "far_moved_from_detection": _gate_far_moved_from_detection(
            row, max_move_pct=max_move_pct
        ),
        "no_venue_route": _gate_no_venue_route(row),
        "liquidity_unavailable": _gate_liquidity_unavailable(row),
    }


def build_report(
    endpoint_url: str,
    rows: list[dict],
    now: datetime,
    *,
    stale_hours: float,
    max_age_hours: float,
    max_move_pct: float,
    top_n: int = TOP_N,
    window_hours: int = 36,
) -> dict:
    """Pure report builder. No I/O. Returns a JSON-serialisable dict.

    Anti-scope (verifiable): consumes endpoint order as-is, only COUNTS, never
    re-orders rows, returns no ordered list of rows — counts/rates only.
    """
    total = len(rows)
    top_denom = min(top_n, total)

    # Pre-evaluate every row once.
    evaluations: list[dict[str, Optional[bool]]] = [
        _evaluate_row(
            row,
            stale_hours=stale_hours,
            max_age_hours=max_age_hours,
            max_move_pct=max_move_pct,
        )
        for row in rows
    ]
    top_evaluations = evaluations[:top_n]

    per_gate: dict[str, dict] = {}
    field_findings: dict[str, dict] = {}
    schema_findings: list[str] = []

    for gate in ALL_GATES:
        excluded_count = sum(1 for ev in evaluations if ev[gate] is True)
        evaluable_count = sum(1 for ev in evaluations if ev[gate] is not None)
        missing_count = sum(1 for ev in evaluations if ev[gate] is None)
        topn_removed = sum(1 for ev in top_evaluations if ev[gate] is True)

        status = "evaluable" if evaluable_count > 0 else "not_evaluable"
        per_gate[gate] = {
            "excluded_count": excluded_count,
            "evaluable_count": evaluable_count,
            "excluded_rate": _rate_or_null(excluded_count, evaluable_count),
            "topN_removed": topn_removed,
            "topN_removed_rate": _rate_or_null(topn_removed, top_denom),
            "status": status,
        }

        field_findings[gate] = {
            "field_checked": GATE_FIELD[gate],
            "rows_missing_field": missing_count,
            "rows_missing_rate": _rate_or_null(missing_count, total),
        }

        if missing_count == total and total > 0:
            schema_findings.append(
                f"gate '{gate}': field '{GATE_FIELD[gate]}' absent/None on "
                f"{missing_count}/{total} rows -> NOT-EVALUABLE"
            )
        elif missing_count > 0:
            schema_findings.append(
                f"gate '{gate}': field '{GATE_FIELD[gate]}' missing/None on "
                f"{missing_count}/{total} rows"
            )

    # Combined survivors over EVALUABLE gates only.
    survivors_count = 0
    unknown_rows = 0
    top_survivors = 0
    for idx, ev in enumerate(evaluations):
        has_unknown_evaluable = any(ev[g] is None for g in EVALUABLE_GATES)
        survives = all(ev[g] is False for g in EVALUABLE_GATES)
        if has_unknown_evaluable:
            unknown_rows += 1
        if survives:
            survivors_count += 1
            if idx < top_n:
                top_survivors += 1

    combined = {
        "survivors_count": survivors_count,
        "survivors_rate": _rate_or_null(survivors_count, total),
        "dropped_count": total - survivors_count,
        "topN_survivors": top_survivors,
        "unknown_rows": unknown_rows,
    }

    return {
        "audited_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "endpoint": endpoint_url,
        "params": {
            "window_hours": window_hours,
            "stale_hours": stale_hours,
            "max_age_hours": max_age_hours,
            "max_move_pct": max_move_pct,
            "top_n": top_n,
        },
        "total_rows": total,
        "top_n": top_n,
        "per_gate": per_gate,
        "combined": combined,
        "field_findings": field_findings,
        "schema_findings": schema_findings,
    }


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "OFFLINE read-only freshness/tradability gate diagnostic for "
            "Today's Focus."
        ),
    )
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--window-hours", type=int, default=36)
    parser.add_argument("--stale-hours", type=float, default=24.0)
    parser.add_argument("--max-age-hours", type=float, default=12.0)
    parser.add_argument("--max-move-pct", type=float, default=150.0)
    parser.add_argument("--top-n", type=int, default=TOP_N)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--json", action="store_true")
    return parser


def _emit_error(message: str, *, stage: str, as_json: bool) -> int:
    """Print an error and return exit code 2 (mirrors --json error envelope)."""
    if as_json:
        print(json.dumps({"status": "error", "stage": stage, "error": message}))
    else:
        print(f"error: {message}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    # argparse-native errors (bad type, etc.) raise SystemExit(2) directly.
    args = parser.parse_args(argv)

    as_json = args.json
    if args.window_hours < 6 or args.window_hours > 72:
        return _emit_error(
            "--window-hours must be in [6, 72]", stage="args", as_json=as_json
        )
    if args.stale_hours <= 0:
        return _emit_error("--stale-hours must be > 0", stage="args", as_json=as_json)
    if args.max_age_hours <= 0:
        return _emit_error("--max-age-hours must be > 0", stage="args", as_json=as_json)
    if args.max_move_pct <= 0:
        return _emit_error("--max-move-pct must be > 0", stage="args", as_json=as_json)
    if args.top_n <= 0:
        return _emit_error("--top-n must be > 0", stage="args", as_json=as_json)
    if args.timeout <= 0:
        return _emit_error("--timeout must be > 0", stage="args", as_json=as_json)

    endpoint_url = f"{args.url}{ENDPOINT_PATH}?window_hours={args.window_hours}"
    try:
        rows = _fetch_focus_rows(endpoint_url, args.timeout)
    except (
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        return _emit_error(
            f"failed to fetch focus rows: {exc}", stage="fetch", as_json=as_json
        )

    report = build_report(
        endpoint_url,
        rows,
        datetime.now(timezone.utc),
        stale_hours=args.stale_hours,
        max_age_hours=args.max_age_hours,
        max_move_pct=args.max_move_pct,
        top_n=args.top_n,
        window_hours=args.window_hours,
    )
    if as_json:
        print(json.dumps(report, indent=2))
    else:
        _print_human(report)
        print()
        print(json.dumps(report, indent=2))
    return 0


def _fmt_rate(rate: Optional[float]) -> str:
    return "n/a" if rate is None else f"{rate:.1%}"


def _print_human(report: dict) -> None:
    print("# Today's Focus — freshness/tradability gate audit (OFFLINE, read-only)")
    print()
    print(f"- audited_at: {report['audited_at']}")
    print(f"- endpoint:   {report['endpoint']}")
    print(f"- total_rows: {report['total_rows']} (top_n={report['top_n']})")
    print()
    print("## Per-gate (DIAGNOSTIC — counts only, no ranking)")
    print()
    print("| Gate | Status | Excluded | Evaluable | Excl rate | topN removed |")
    print("|---|---|---:|---:|---:|---:|")
    for gate in ALL_GATES:
        g = report["per_gate"][gate]
        print(
            f"| {gate} | {g['status']} | {g['excluded_count']} | "
            f"{g['evaluable_count']} | {_fmt_rate(g['excluded_rate'])} | "
            f"{g['topN_removed']} |"
        )
    combined = report["combined"]
    print()
    print(
        f"- survives ALL EVALUABLE gates: {combined['survivors_count']} "
        f"({_fmt_rate(combined['survivors_rate'])}); "
        f"dropped {combined['dropped_count']}; "
        f"unknown {combined['unknown_rows']}"
    )
    if report["schema_findings"]:
        print()
        print("## Schema findings")
        for line in report["schema_findings"]:
            print(f"- {line}")


if __name__ == "__main__":
    raise SystemExit(main())
