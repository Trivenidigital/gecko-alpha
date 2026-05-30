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
import math
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
    """Return numerator/denominator (rounded 4dp), or None when denom is zero."""
    if denominator <= 0:
        return None
    return round(numerator / denominator, 4)


def _coerce_number(value: Any) -> Optional[float]:
    """Return ``value`` as a float, or None if missing / non-numeric.

    A bool is intentionally rejected (a numeric gate over a bool would be
    meaningless), so ``True``/``False`` are treated as non-numeric here. This
    lets the numeric gates treat a non-numeric value (e.g. a stray string) as
    "missing" — surfaced in field_findings — instead of crashing on a
    comparison.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _fetch_focus_rows(endpoint_url: str, timeout: float) -> list[dict]:
    """GET the endpoint and return the rows list (raises on any failure)."""
    req = urllib.request.Request(endpoint_url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return _extract_rows(payload)


def _extract_rows(payload: Any) -> list[dict]:
    """Validate the parsed payload shape and return its ``rows`` list.

    The endpoint envelope is ``{"meta": {...}, "rows": [...]}``. A payload whose
    shape does NOT match this — a dict missing the ``rows`` key (a schema
    change, a ``{"data": [...]}`` envelope, or a 200 *error* envelope), or a
    ``rows`` value that is not a list (``null``, a string, an object) — is a
    malformed/unexpected payload. It is a fetch/schema FAILURE (-> exit 2,
    stage="fetch"), NOT a genuinely-empty cohort. Treating it as an empty
    cohort would yield a silent all-zero report + exit 0, which is exactly the
    silent-failure class this diagnostic exists to surface.

    The single VALID empty case is ``{"rows": []}`` — the key is present and is
    an empty list — which returns ``[]`` (exit 0, total_rows 0). The
    distinction is "``rows`` key present AND is a list" (valid, possibly empty)
    vs "``rows`` key absent OR ``rows`` not a list" (malformed -> raise).
    """
    if not isinstance(payload, dict):
        raise ValueError(
            f"endpoint returned a non-object payload ({type(payload).__name__}); "
            "expected an object with a 'rows' list"
        )
    if "rows" not in payload:
        raise ValueError(
            "endpoint payload is missing the 'rows' key "
            f"(keys present: {sorted(payload.keys())}) — unexpected/error envelope"
        )
    rows = payload["rows"]
    if not isinstance(rows, list):
        raise ValueError(f"endpoint 'rows' is not a list ({type(rows).__name__})")
    return rows


# --------------------------------------------------------------------------- #
# Gate predicates: excluded(row) -> True | False | None                       #
#   True  = row WOULD be excluded by this gate                                 #
#   False = row survives this gate                                             #
#   None  = required field absent -> not evaluable (counted in field_findings) #
# --------------------------------------------------------------------------- #


def _stale_fallback_kind(row: dict) -> Optional[str]:
    """Classify how the stale gate falls back to the price_is_stale bool.

    The bool fallback fires whenever the PRIMARY ``price_staleness_minutes``
    field does not yield a usable number AND ``price_is_stale`` is present (not
    absent/None). When it fires, the ``--stale-hours`` threshold cannot be
    honoured (a bool carries no minutes), so the threshold is effectively
    bypassed for that row — surfaced in field_findings so the bypass is visible
    rather than silent.

    Returns:
        ``"missing"``      — primary absent/None, bool fallback used.
        ``"non_numeric"``  — primary PRESENT but non-numeric (e.g. a stray
                             string), bool fallback used. Distinct from
                             ``"missing"`` so the report can show the field WAS
                             emitted but was unusable, vs simply absent.
        ``"non_bool"``     — primary unusable AND ``price_is_stale`` is present
                             but is NOT a bool (e.g. the string ``"false"``).
                             The bool fallback REFUSES a non-bool value (it
                             would be silently truthy), so the gate is
                             unevaluable for that row — surfaced, not silent.
        ``None``           — no fallback (primary usable, or no bool to fall
                             back to).
    """
    if not isinstance(row, dict):
        return None
    flag = row.get("price_is_stale", _MISSING)
    if flag is _MISSING or flag is None:
        return None  # nothing to fall back to -> gate is just not-evaluable.
    minutes = row.get("price_staleness_minutes", _MISSING)
    primary_usable = (
        minutes is not _MISSING
        and minutes is not None
        and _coerce_number(minutes) is not None
    )
    if primary_usable:
        return None  # primary is a usable number -> no fallback.
    # Primary is unusable -> we WOULD fall back to the bool. Classify the bool.
    if not isinstance(flag, bool):
        # price_is_stale present but NOT a bool (e.g. the string "false", which
        # is truthy). bool(flag) here would silently mark the row stale; refuse
        # the fallback and surface it instead -> gate unevaluable for this row.
        return "non_bool"
    if minutes is _MISSING or minutes is None:
        return "missing"
    # primary present but non-numeric (or a bool) -> coerces to None -> the gate
    # falls back to the bool, bypassing --stale-hours silently unless surfaced.
    return "non_numeric"


def _gate_stale_price(row: dict, *, stale_hours: float) -> Optional[bool]:
    if not isinstance(row, dict):
        return None
    minutes = _coerce_number(row.get("price_staleness_minutes", _MISSING))
    if minutes is None:
        # Primary absent/None/non-numeric -> fall back to the server's boolean
        # staleness flag if present (the --stale-hours threshold is bypassed
        # for this row; surfaced in field_findings via _stale_uses_bool_fallback).
        flag = row.get("price_is_stale", _MISSING)
        if flag is _MISSING or flag is None:
            return None
        if not isinstance(flag, bool):
            # price_is_stale present but NOT a bool (e.g. the string "false",
            # which is truthy). Refuse the fallback rather than silently bool()
            # it into a stale exclusion -> gate unevaluable for this row,
            # surfaced via field_findings.stale_price.bool_fallback_non_bool.
            return None
        return flag
    return minutes >= stale_hours * 60.0


def _gate_missing_detected_price(row: dict) -> Optional[bool]:
    if not isinstance(row, dict):
        return None
    move = row.get("current_move_pct", _MISSING)
    if move is _MISSING:
        return None
    # current_move_pct is None EXACTLY when entry/current price was missing.
    return move is None


def _gate_too_old_since_detection(row: dict, *, max_age_hours: float) -> Optional[bool]:
    if not isinstance(row, dict):
        return None
    raw = row.get("opened_age_hours", _MISSING)
    if raw is _MISSING:
        return None
    age = _coerce_number(raw)
    if age is None:
        # present-but-None or present-but-non-numeric -> not evaluable for this
        # row (counted in field_findings, never crashes on the comparison).
        return None
    return age > max_age_hours


def _gate_far_moved_from_detection(row: dict, *, max_move_pct: float) -> Optional[bool]:
    if not isinstance(row, dict):
        return None
    raw = row.get("current_move_pct", _MISSING)
    if raw is _MISSING or raw is None:
        return None
    move = _coerce_number(raw)
    if move is None:
        # present-but-non-numeric -> not evaluable for this row (no crash).
        return None
    return abs(move) > max_move_pct


def _gate_no_venue_route(row: dict) -> Optional[bool]:
    if not isinstance(row, dict):
        return None
    # chart_url is NOT emitted on the focus row -> not-evaluable on every row.
    val = row.get("chart_url", _MISSING)
    if val is _MISSING:
        return None
    return val is None or val == ""


def _gate_liquidity_unavailable(row: dict) -> Optional[bool]:
    if not isinstance(row, dict):
        return None
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

    # Count non-dict (malformed) row elements: a row that is not a dict cannot
    # be evaluated by any gate (every predicate returns None for it) and would,
    # before this guard, have crashed on row.get(...). Surface-and-count rather
    # than crash (exit 1) or raise (exit 2) -> the more diagnostic option.
    malformed_rows = sum(1 for row in rows if not isinstance(row, dict))

    # Stale gate: rows where the PRIMARY price_staleness_minutes does not yield
    # a usable number but the price_is_stale bool fallback fires -> --stale-hours
    # bypassed. Split by WHY the primary was unusable so the report distinguishes
    # "field absent/None" from "field present but non-numeric".
    stale_fallback_kinds = [_stale_fallback_kind(row) for row in rows]
    stale_bool_fallback_missing_rows = stale_fallback_kinds.count("missing")
    stale_bool_fallback_non_numeric_rows = stale_fallback_kinds.count("non_numeric")
    # Rows where price_is_stale is present but NOT a bool: the fallback is
    # refused (gate unevaluable for the row) rather than silently bool()'d.
    stale_bool_fallback_non_bool_rows = stale_fallback_kinds.count("non_bool")

    # Numeric gates: rows where the field is present-but-non-numeric (treated as
    # missing rather than crashing the comparison).
    age_non_numeric = sum(
        1
        for row in rows
        if isinstance(row, dict)
        and row.get("opened_age_hours", _MISSING) is not _MISSING
        and row.get("opened_age_hours") is not None
        and _coerce_number(row.get("opened_age_hours")) is None
    )
    move_non_numeric = sum(
        1
        for row in rows
        if isinstance(row, dict)
        and row.get("current_move_pct", _MISSING) is not _MISSING
        and row.get("current_move_pct") is not None
        and _coerce_number(row.get("current_move_pct")) is None
    )

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
        # Evaluability of the TOP-N SLICE specifically (not the global cohort):
        # how many of the first top_n rows carry a non-None verdict for this
        # gate. If NONE are evaluable the slice's removal is UNKNOWN even when a
        # later row makes the gate globally evaluable.
        topn_slice_evaluable = sum(1 for ev in top_evaluations if ev[gate] is not None)

        status = "evaluable" if evaluable_count > 0 else "not_evaluable"
        if topn_slice_evaluable == 0:
            # The top-N slice cannot be evaluated on any of its rows, so its
            # removal is UNKNOWN, not zero. Report both topN_removed and
            # topN_removed_rate as null -> a 0 / 0.0 would falsely read as
            # "nothing removed in the top slice" when the truth is "we could not
            # look at the top slice". This holds whether the gate is globally
            # not_evaluable OR globally evaluable via a row beyond the top-N.
            topn_removed_out: Optional[int] = None
            topn_removed_rate_out = None
        else:
            # Rate is "removed among the EVALUABLE top-N rows", so the
            # denominator is topn_slice_evaluable, NOT top_denom. A PARTIALLY
            # unevaluable slice (e.g. 1 evaluable+removed + 4 unevaluable in
            # top-5) over top_denom would dilute the rate to 0.2 and silently
            # treat the 4 unknowns as "not removed". Over the evaluable subset it
            # is 1/1 = 1.0; the unknown top-N rows are disclosed separately via
            # topN_evaluable < top_n.
            topn_removed_out = topn_removed
            topn_removed_rate_out = _rate_or_null(topn_removed, topn_slice_evaluable)

        per_gate[gate] = {
            "excluded_count": excluded_count,
            "evaluable_count": evaluable_count,
            "excluded_rate": _rate_or_null(excluded_count, evaluable_count),
            "topN_removed": topn_removed_out,
            "topN_evaluable": topn_slice_evaluable,
            "topN_removed_rate": topn_removed_rate_out,
            "status": status,
        }

        gate_finding = {
            "field_checked": GATE_FIELD[gate],
            "rows_missing_field": missing_count,
            "rows_missing_rate": _rate_or_null(missing_count, total),
        }
        if malformed_rows:
            gate_finding["malformed_rows"] = malformed_rows
        if gate == "stale_price" and stale_bool_fallback_missing_rows:
            gate_finding["primary_field_missing_used_bool_fallback"] = (
                stale_bool_fallback_missing_rows
            )
        if gate == "stale_price" and stale_bool_fallback_non_numeric_rows:
            gate_finding["primary_field_non_numeric_used_bool_fallback"] = (
                stale_bool_fallback_non_numeric_rows
            )
        if gate == "stale_price" and stale_bool_fallback_non_bool_rows:
            gate_finding["bool_fallback_non_bool"] = stale_bool_fallback_non_bool_rows
        if gate == "too_old_since_detection" and age_non_numeric:
            gate_finding["rows_non_numeric"] = age_non_numeric
        if gate == "far_moved_from_detection" and move_non_numeric:
            gate_finding["rows_non_numeric"] = move_non_numeric
        field_findings[gate] = gate_finding

        if gate == "stale_price" and stale_bool_fallback_missing_rows:
            schema_findings.append(
                f"gate 'stale_price': {stale_bool_fallback_missing_rows}/{total} rows "
                "missing primary 'price_staleness_minutes' -> used 'price_is_stale' "
                "bool fallback (--stale-hours threshold bypassed for those rows)"
            )
        if gate == "stale_price" and stale_bool_fallback_non_numeric_rows:
            schema_findings.append(
                f"gate 'stale_price': {stale_bool_fallback_non_numeric_rows}/{total} "
                "rows had non-numeric primary 'price_staleness_minutes' -> used "
                "'price_is_stale' bool fallback (--stale-hours threshold bypassed "
                "for those rows)"
            )
        if gate == "stale_price" and stale_bool_fallback_non_bool_rows:
            schema_findings.append(
                f"gate 'stale_price': {stale_bool_fallback_non_bool_rows}/{total} "
                "rows had a non-bool 'price_is_stale' fallback value -> fallback "
                "refused, gate NOT-EVALUABLE for those rows (a non-bool would be "
                "silently truthy)"
            )
        if gate == "too_old_since_detection" and age_non_numeric:
            schema_findings.append(
                f"gate 'too_old_since_detection': field 'opened_age_hours' "
                f"non-numeric on {age_non_numeric}/{total} rows (treated as missing)"
            )
        if gate == "far_moved_from_detection" and move_non_numeric:
            schema_findings.append(
                f"gate 'far_moved_from_detection': field 'current_move_pct' "
                f"non-numeric on {move_non_numeric}/{total} rows (treated as missing)"
            )

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

    # Combined survivors over EVALUABLE gates only. A non-dict (malformed) row
    # has every gate == None, so it is never a survivor and is tallied in
    # unknown_rows; it is additionally surfaced via combined.malformed_rows.
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
        "malformed_rows": malformed_rows,
    }
    if malformed_rows:
        schema_findings.append(
            f"{malformed_rows}/{total} rows were non-dict (malformed) and could "
            "not be evaluated by any gate (skipped, counted as unknown)"
        )

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
    # Float thresholds must be FINITE and positive. argparse type=float happily
    # accepts "nan"/"inf"/"-inf"; a bare `<= 0` check passes NaN (all NaN
    # comparisons are False) and +inf, which would silently disable the gate.
    for name, value in (
        ("--stale-hours", args.stale_hours),
        ("--max-age-hours", args.max_age_hours),
        ("--max-move-pct", args.max_move_pct),
        ("--timeout", args.timeout),
    ):
        if not (math.isfinite(value) and value > 0):
            return _emit_error(
                f"{name} must be a finite number > 0", stage="args", as_json=as_json
            )
    if args.top_n <= 0:
        return _emit_error("--top-n must be > 0", stage="args", as_json=as_json)

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
