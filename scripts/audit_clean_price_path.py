#!/usr/bin/env python3
"""Clean price-path runner-attribution audit (offline hindsight diagnostic).

OFFLINE-ONLY. This script intentionally consumes post-detection
(future-relative-to-detection) price data BECAUSE it is offline hindsight
attribution. It MUST NOT feed live ranking, curation, alerting, sizing, or
signal enable/disable. Any such use requires a separate no-lookahead design
(per BL-NEW-CLEAN-PRICE-PATH-AUDIT: "Keep output offline; no live ranking or
curation changes without a follow-up design").

For candidates that later ran, it classifies the post-detection price path
into usable movement buckets — continuous_move, drawdown_then_recovery, or
unrelated_later_move — plus the residual buckets no_significant_move,
insufficient_data, and window_incomplete.

Source-of-truth scope: ``volume_history_cg`` ONLY (the markets-watcher cadence
source; ``scout/spikes/detector.py`` writes (coin_id, price, recorded_at)).
The writer prunes rows older than 7 days, so ``--window-hours`` is capped at
168. Genuine "runs weeks later" catalysts are UNOBSERVABLE within this
retention: a candidate flat for its whole 7-day window lands in
no_significant_move, NOT window_incomplete/insufficient_data.
unrelated_later_move captures only flat-then-run ENTIRELY WITHIN the 7-day
window; weeks-later recurrence requires a longer-retention source (out of V1
scope). Mirrors the conventions of ``scripts/audit_price_path_coverage.py``.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

WINDOW_HOURS_CEILING = 168  # 7d writer retention; rows older are pruned.
INFINITY_GUARD_MAX = 1e308  # defensive ceiling against +Inf in REAL columns.
MATURED_RATE_MIN_DENOM = 5  # N<5 convention applied at the rate-denominator level.

OFFLINE_ONLY_BANNER = (
    "OFFLINE-ONLY hindsight attribution. MUST NOT feed live "
    "ranking/curation/alerting/sizing. See BL-NEW-CLEAN-PRICE-PATH-AUDIT."
)

# Closed bucket set.
BUCKETS = (
    "continuous_move",
    "drawdown_then_recovery",
    "unrelated_later_move",
    "no_significant_move",
    "insufficient_data",
    "window_incomplete",
)
# Buckets excluded from the matured-rate denominator and whose metrics are null.
METRIC_NULL_BUCKETS = ("insufficient_data", "window_incomplete")

# Sensitivity sweep (fold #7).
SENS_RUN_THRESHOLDS = (20.0, 30.0, 40.0)
SENS_DRAWDOWN_THRESHOLDS = (10.0, 15.0, 20.0)


# --------------------------------------------------------------------------- #
# Small helpers (reference parity)
# --------------------------------------------------------------------------- #
def _utc_iso_z(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso_utc(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _is_valid_price(value: Any) -> bool:
    try:
        p = float(value)
    except (TypeError, ValueError):
        return False
    return 0.0 < p < INFINITY_GUARD_MAX


def _rate_or_null(num: int, denom: int) -> float | None:
    if denom <= 0:
        return None
    return round(num / denom, 4)


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    try:
        cursor = conn.execute(f"PRAGMA table_info({table})")
    except sqlite3.Error:
        return False
    return any(row[1] == column for row in cursor.fetchall())


def _identity_class(identity: str) -> str:
    """Cheap offline heuristic (no network): coarse identity-shape class."""
    s = (identity or "").strip()
    if not s:
        return "other"
    low = s.lower()
    if low.startswith("0x") and len(s) == 42:
        try:
            int(s[2:], 16)
            return "contract_address_like"
        except ValueError:
            pass
    # base58-ish (Solana mint): 32-44 chars, no 0/O/I/l, alphanumeric.
    if 32 <= len(s) <= 44 and s.isalnum() and all(c not in "0OIl" for c in s):
        return "contract_address_like"
    if "-" in s or (s.isalnum() and s.islower() and len(s) < 32):
        return "cg_slug_like"
    return "other"


# --------------------------------------------------------------------------- #
# Price series + excursion math
# --------------------------------------------------------------------------- #
def _price_series(
    conn: sqlite3.Connection,
    coin_id: str,
    start_dt: datetime,
    end_dt: datetime,
) -> list[tuple[datetime, float]]:
    """Valid price points in [start_dt, end_dt], inclusive both ends, sorted."""
    if not coin_id:
        return []
    try:
        cursor = conn.execute(
            "SELECT recorded_at, price FROM volume_history_cg "
            "WHERE coin_id = ? AND recorded_at >= ? AND recorded_at <= ? "
            "AND price IS NOT NULL AND price > 0 AND price < ? "
            "ORDER BY recorded_at ASC",
            (coin_id, start_dt.isoformat(), end_dt.isoformat(), INFINITY_GUARD_MAX),
        )
    except sqlite3.Error:
        return []
    out: list[tuple[datetime, float]] = []
    for recorded_at, price in cursor.fetchall():
        if not _is_valid_price(price):
            continue
        try:
            out.append((_parse_iso_utc(recorded_at), float(price)))
        except (TypeError, ValueError):
            continue
    return out


def _longest_flat_run_hours(
    points: list[tuple[datetime, float]], p0: float, band_pct: float
) -> float:
    """Longest contiguous span where every point stays within +/- band_pct of P0."""
    longest = 0.0
    span_start: datetime | None = None
    span_last: datetime | None = None
    span_count = 0
    for dt, price in points:
        flat = abs((price - p0) / p0 * 100.0) <= band_pct
        if flat:
            if span_start is None:
                span_start = dt
                span_count = 1
            else:
                span_count += 1
            span_last = dt
            if span_count >= 2:
                span_h = (span_last - span_start).total_seconds() / 3600.0
                longest = max(longest, span_h)
        else:
            span_start = None
            span_last = None
            span_count = 0
    return longest


def _classify_one(
    candidate: dict,
    conn: sqlite3.Connection,
    *,
    window_hours: int,
    run_threshold: float,
    drawdown_threshold: float,
    flat_gap_hours: float,
    flat_band_pct: float,
    min_points: int,
    maturity_hours: float,
    now: datetime,
) -> dict[str, Any]:
    coin_id = candidate.get("coin_id") or ""
    source = candidate.get("cohort_source") or "paper"
    detection_ts = candidate.get("detection_ts") or ""

    base = {
        "coin_id": coin_id,
        "cohort_source": source,
        "mfe": None,
        "mae": None,
        "time_to_peak": None,
        "p0_basis": None,
    }

    try:
        detection_dt = _parse_iso_utc(detection_ts)
    except (TypeError, ValueError):
        return {**base, "bucket": "insufficient_data"}

    # ---- maturity guard ----
    if detection_dt + timedelta(hours=maturity_hours) > now:
        return {**base, "bucket": "window_incomplete"}

    # ---- establish P0 ----
    series = _price_series(
        conn, coin_id, detection_dt, detection_dt + timedelta(hours=window_hours)
    )
    p0 = candidate.get("detected_price")
    if _is_valid_price(p0):
        p0 = float(p0)
        p0_basis = "ledger_detected_price"
    elif series:
        p0 = series[0][1]
        p0_basis = "first_in_window_point"
    else:
        return {**base, "bucket": "insufficient_data"}

    if len(series) < min_points:
        return {**base, "bucket": "insufficient_data", "p0_basis": p0_basis}

    # ---- excursions ----
    peak_price = max(p for _, p in series)
    peak_dt = next(dt for dt, p in series if p == peak_price)
    mfe_pct = (peak_price - p0) / p0 * 100.0
    time_to_peak_h = (peak_dt - detection_dt).total_seconds() / 3600.0

    pre_peak = [(dt, p) for dt, p in series if dt <= peak_dt]
    trough_price = min(p for _, p in pre_peak)
    mae_before_favorable_pct = (p0 - trough_price) / p0 * 100.0

    flat_gap_h = _longest_flat_run_hours(pre_peak, p0, flat_band_pct)

    metrics = {
        "mfe": round(mfe_pct, 4),
        "mae": round(mae_before_favorable_pct, 4),
        "time_to_peak": round(time_to_peak_h, 4),
        "p0_basis": p0_basis,
    }

    # ---- decision tree (order is load-bearing) ----
    if mfe_pct < run_threshold:
        bucket = "no_significant_move"
    elif flat_gap_h >= flat_gap_hours:
        bucket = "unrelated_later_move"
    elif mae_before_favorable_pct <= drawdown_threshold:
        bucket = "continuous_move"
    else:
        bucket = "drawdown_then_recovery"

    return {"coin_id": coin_id, "cohort_source": source, "bucket": bucket, **metrics}


def _bucket_counts(per_row: list[dict]) -> dict[str, int]:
    counts = {b: 0 for b in BUCKETS}
    for r in per_row:
        counts[r["bucket"]] = counts.get(r["bucket"], 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# Fold #5 — gainers runner-def crosscheck
# --------------------------------------------------------------------------- #
def _gainers_crosscheck(
    cohort_rows: list[dict], per_row: list[dict], run_threshold: float
) -> dict[str, Any]:
    by_id = {r["coin_id"]: r for r in per_row}
    compared = []
    audit_ran = stored_ran = agree = dis_no_yes = dis_yes_no = 0
    for cand in cohort_rows:
        if (cand.get("cohort_source") or "") != "gainers":
            continue
        stored = cand.get("stored_peak_gain_pct")
        if stored is None:
            continue
        row = by_id.get(cand.get("coin_id"))
        audit_mfe = row.get("mfe") if row else None
        a_ran = audit_mfe is not None and audit_mfe >= run_threshold
        s_ran = float(stored) >= run_threshold
        if a_ran:
            audit_ran += 1
        if s_ran:
            stored_ran += 1
        if a_ran == s_ran:
            agree += 1
        elif s_ran and not a_ran:
            dis_no_yes += 1
        else:
            dis_yes_no += 1
        compared.append(
            {
                "coin_id": cand.get("coin_id"),
                "audit_mfe": audit_mfe,
                "stored_peak_gain_pct": float(stored),
                "audit_ran": a_ran,
                "stored_ran": s_ran,
            }
        )
    return {
        "rows_compared": len(compared),
        "audit_ran_count": audit_ran,
        "stored_ran_count": stored_ran,
        "agree_count": agree,
        "disagree_audit_no_stored_yes": dis_no_yes,
        "disagree_audit_yes_stored_no": dis_yes_no,
        "per_row": compared,
    }


# --------------------------------------------------------------------------- #
# Fold #4 — join-failure breakdown
# --------------------------------------------------------------------------- #
def _join_failure_breakdown(
    cohort_rows: list[dict], per_row: list[dict]
) -> dict[str, Any]:
    by_source = {"paper": 0, "gainers": 0}
    by_class = {"cg_slug_like": 0, "contract_address_like": 0, "other": 0}
    zero_in_window = 0
    p0_unresolvable = 0
    cand_by_id = {c.get("coin_id"): c for c in cohort_rows}
    insufficient = [r for r in per_row if r["bucket"] == "insufficient_data"]
    for r in insufficient:
        cid = r["coin_id"]
        cand = cand_by_id.get(cid, {})
        src = cand.get("cohort_source") or "paper"
        by_source[src] = by_source.get(src, 0) + 1
        by_class[_identity_class(cid)] += 1
        if r.get("p0_basis") is None:
            p0_unresolvable += 1
        else:
            zero_in_window += 1
    return {
        "insufficient_data_total": len(insufficient),
        "by_cohort_source": by_source,
        "by_identity_class": by_class,
        "zero_in_window_points": zero_in_window,
        "p0_unresolvable": p0_unresolvable,
    }


# --------------------------------------------------------------------------- #
# Schema findings (PRAGMA-driven, reference parity)
# --------------------------------------------------------------------------- #
def _schema_findings(conn: sqlite3.Connection) -> dict[str, Any]:
    return {
        "volume_history_cg_has_price": _column_exists(
            conn, "volume_history_cg", "price"
        ),
        "volume_history_cg_has_recorded_at": _column_exists(
            conn, "volume_history_cg", "recorded_at"
        ),
        "volume_history_cg_has_coin_id": _column_exists(
            conn, "volume_history_cg", "coin_id"
        ),
        "paper_trades_present": _column_exists(conn, "paper_trades", "token_id"),
        "paper_trades_detection_ts_column": (
            "opened_at" if _column_exists(conn, "paper_trades", "opened_at") else None
        ),
        "paper_trades_detected_price_column": (
            "entry_price"
            if _column_exists(conn, "paper_trades", "entry_price")
            else None
        ),
        "paper_trades_coin_id_column": (
            "token_id" if _column_exists(conn, "paper_trades", "token_id") else None
        ),
        "gainers_comparisons_present": _column_exists(
            conn, "gainers_comparisons", "coin_id"
        ),
        "gainers_comparisons_detected_price_column": (
            "detected_price"
            if _column_exists(conn, "gainers_comparisons", "detected_price")
            else None
        ),
        "gainers_comparisons_detection_ts_column": (
            "appeared_on_gainers_at"
            if _column_exists(conn, "gainers_comparisons", "appeared_on_gainers_at")
            else None
        ),
        "gainers_comparisons_coin_id_column": (
            "coin_id"
            if _column_exists(conn, "gainers_comparisons", "coin_id")
            else None
        ),
        "gainers_comparisons_has_peak_gain_pct": _column_exists(
            conn, "gainers_comparisons", "peak_gain_pct"
        ),
    }


# --------------------------------------------------------------------------- #
# Pure core
# --------------------------------------------------------------------------- #
def build_report(
    cohort_rows: list[dict],
    conn: sqlite3.Connection,
    *,
    window_hours: int,
    run_threshold: float,
    drawdown_threshold: float,
    flat_gap_hours: float,
    flat_band_pct: float,
    min_points: int,
    maturity_hours: float,
    now: datetime,
    sensitivity: bool = False,
    cohort: str = "both",
    lookback_days: int = 30,
) -> dict[str, Any]:
    per_row = [
        _classify_one(
            cand,
            conn,
            window_hours=window_hours,
            run_threshold=run_threshold,
            drawdown_threshold=drawdown_threshold,
            flat_gap_hours=flat_gap_hours,
            flat_band_pct=flat_band_pct,
            min_points=min_points,
            maturity_hours=maturity_hours,
            now=now,
        )
        for cand in cohort_rows
    ]
    counts = _bucket_counts(per_row)
    total = len(per_row)

    gross = {b: _rate_or_null(counts[b], total) for b in BUCKETS} if total > 0 else None
    matured_denom = total - counts["window_incomplete"] - counts["insufficient_data"]
    if matured_denom < MATURED_RATE_MIN_DENOM:
        matured = None
        suppressed_reason = (
            f"matured_denominator < {MATURED_RATE_MIN_DENOM}"
            if total > 0
            else "matured_denominator == 0"
        )
    else:
        matured = {
            b: _rate_or_null(counts[b], matured_denom)
            for b in BUCKETS
            if b not in METRIC_NULL_BUCKETS
        }
        suppressed_reason = None

    report: dict[str, Any] = {
        "audited_at": _utc_iso_z(now),
        "offline_only_banner": OFFLINE_ONLY_BANNER,
        "params": {
            "cohort": cohort,
            "window_hours": window_hours,
            "run_threshold": run_threshold,
            "drawdown_threshold": drawdown_threshold,
            "flat_gap_hours": flat_gap_hours,
            "flat_band_pct": flat_band_pct,
            "min_points": min_points,
            "maturity_hours": maturity_hours,
            "lookback_days": lookback_days,
        },
        "total_cohort": total,
        "bucket_counts": counts,
        "bucket_rates_gross": gross,
        "bucket_rates_matured": matured,
        "matured_denominator": matured_denom,
        "bucket_rates_matured_suppressed_reason": suppressed_reason,
        "per_row": per_row,
        "join_failure_breakdown": _join_failure_breakdown(cohort_rows, per_row),
        "gainers_runner_def_crosscheck": _gainers_crosscheck(
            cohort_rows, per_row, run_threshold
        ),
        "schema_findings": _schema_findings(conn),
    }

    if sensitivity:
        report["sensitivity"] = _sensitivity_block(
            cohort_rows,
            conn,
            window_hours=window_hours,
            flat_gap_hours=flat_gap_hours,
            flat_band_pct=flat_band_pct,
            min_points=min_points,
            maturity_hours=maturity_hours,
            now=now,
        )
    return report


def _sensitivity_block(
    cohort_rows: list[dict],
    conn: sqlite3.Connection,
    *,
    window_hours: int,
    flat_gap_hours: float,
    flat_band_pct: float,
    min_points: int,
    maturity_hours: float,
    now: datetime,
) -> dict[str, Any]:
    grid = []
    ranges = {b: {"min": None, "max": None} for b in BUCKETS}
    for rt in SENS_RUN_THRESHOLDS:
        for dt_thr in SENS_DRAWDOWN_THRESHOLDS:
            rows = [
                _classify_one(
                    cand,
                    conn,
                    window_hours=window_hours,
                    run_threshold=rt,
                    drawdown_threshold=dt_thr,
                    flat_gap_hours=flat_gap_hours,
                    flat_band_pct=flat_band_pct,
                    min_points=min_points,
                    maturity_hours=maturity_hours,
                    now=now,
                )
                for cand in cohort_rows
            ]
            counts = _bucket_counts(rows)
            grid.append(
                {
                    "run_threshold": rt,
                    "drawdown_threshold": dt_thr,
                    "bucket_counts": counts,
                }
            )
            for b in BUCKETS:
                c = counts[b]
                cur = ranges[b]
                cur["min"] = c if cur["min"] is None else min(cur["min"], c)
                cur["max"] = c if cur["max"] is None else max(cur["max"], c)
    return {
        "run_threshold_sweep": list(SENS_RUN_THRESHOLDS),
        "drawdown_threshold_sweep": list(SENS_DRAWDOWN_THRESHOLDS),
        "grid": grid,
        "per_bucket_count_range": ranges,
    }


# --------------------------------------------------------------------------- #
# Cohort builder (DB query) — the no-network analogue of the reference's fetch
# --------------------------------------------------------------------------- #
def _build_cohort(
    conn: sqlite3.Connection, cohort: str, lookback_days: int, now: datetime
) -> list[dict]:
    cutoff_iso = (now - timedelta(days=lookback_days)).isoformat()
    rows: list[dict] = []
    if cohort in ("paper", "both"):
        cursor = conn.execute(
            "SELECT token_id, opened_at, entry_price FROM paper_trades "
            "WHERE opened_at >= ?",
            (cutoff_iso,),
        )
        for token_id, opened_at, entry_price in cursor.fetchall():
            rows.append(
                {
                    "coin_id": token_id,
                    "detection_ts": opened_at,
                    "detected_price": entry_price,
                    "cohort_source": "paper",
                }
            )
    if cohort in ("gainers", "both"):
        cursor = conn.execute(
            "SELECT coin_id, appeared_on_gainers_at, detected_price, peak_gain_pct "
            "FROM gainers_comparisons WHERE appeared_on_gainers_at >= ?",
            (cutoff_iso,),
        )
        for coin_id, appeared_at, detected_price, peak_gain_pct in cursor.fetchall():
            rows.append(
                {
                    "coin_id": coin_id,
                    "detection_ts": appeared_at,
                    "detected_price": detected_price,
                    "cohort_source": "gainers",
                    "stored_peak_gain_pct": peak_gain_pct,
                }
            )
    return rows


# --------------------------------------------------------------------------- #
# Human format
# --------------------------------------------------------------------------- #
def _format_human(report: dict[str, Any]) -> str:
    lines = [
        f"audited_at:    {report['audited_at']}",
        f"OFFLINE-ONLY:  {report['offline_only_banner']}",
        "params:",
    ]
    for k, v in report["params"].items():
        lines.append(f"  {k:18s}= {v}")
    lines.append(f"total_cohort:  {report['total_cohort']}")
    lines.append("")
    lines.append("BUCKETS (count / gross_rate / matured_rate):")
    matured = report["bucket_rates_matured"]
    gross = report["bucket_rates_gross"]
    for b in BUCKETS:
        g = gross[b] if gross else None
        m = matured[b] if (matured and b in matured) else None
        lines.append(
            f"  {b:24s} count={report['bucket_counts'][b]:4d} gross={g} matured={m}"
        )
    lines.append(f"  matured_denominator = {report['matured_denominator']}")
    if report["bucket_rates_matured_suppressed_reason"]:
        lines.append(
            f"  matured_suppressed: {report['bucket_rates_matured_suppressed_reason']}"
        )
    lines.append("")
    lines.append("JOIN FAILURE BREAKDOWN:")
    for k, v in report["join_failure_breakdown"].items():
        lines.append(f"  {k} = {v}")
    lines.append("")
    lines.append("GAINERS RUNNER-DEF CROSSCHECK:")
    cc = report["gainers_runner_def_crosscheck"]
    for k in (
        "rows_compared",
        "audit_ran_count",
        "stored_ran_count",
        "agree_count",
        "disagree_audit_no_stored_yes",
        "disagree_audit_yes_stored_no",
    ):
        lines.append(f"  {k} = {cc[k]}")
    lines.append("")
    lines.append("PER ROW:")
    for r in report["per_row"]:
        lines.append(
            f"  {r['coin_id']!r} [{r['cohort_source']}] {r['bucket']} "
            f"mfe={r['mfe']} mae={r['mae']} ttp={r['time_to_peak']} p0={r['p0_basis']}"
        )
    if "sensitivity" in report:
        lines.append("")
        lines.append("SENSITIVITY (per_bucket_count_range):")
        for b, rng in report["sensitivity"]["per_bucket_count_range"].items():
            lines.append(f"  {b:24s} min={rng['min']} max={rng['max']}")
    lines.append("")
    lines.append("SCHEMA FINDINGS:")
    for k, v in report["schema_findings"].items():
        lines.append(f"  {k} = {v}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# main()
# --------------------------------------------------------------------------- #
def _err(msg: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(msg))
    else:
        print(f"ERROR: {msg['error']}", file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default="scout.db")
    parser.add_argument(
        "--cohort", choices=("paper", "gainers", "both"), default="both"
    )
    parser.add_argument("--window-hours", type=int, default=168)
    parser.add_argument("--run-threshold", type=float, default=30.0)
    parser.add_argument("--drawdown-threshold", type=float, default=15.0)
    parser.add_argument("--flat-gap-hours", type=float, default=48.0)
    parser.add_argument("--flat-band-pct", type=float, default=10.0)
    parser.add_argument("--min-points", type=int, default=5)
    parser.add_argument("--maturity-hours", type=float, default=None)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--sensitivity", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    maturity_hours = (
        float(args.window_hours) if args.maturity_hours is None else args.maturity_hours
    )

    # ---- argument validation (stage="args", exit 2) ----
    def args_err(error: str) -> int:
        return _err({"status": "error", "stage": "args", "error": error}, args.json)

    if args.window_hours < 1 or args.window_hours > WINDOW_HOURS_CEILING:
        return args_err(
            f"--window-hours must be in [1, {WINDOW_HOURS_CEILING}] "
            "(7-day writer retention ceiling)."
        )
    if args.run_threshold <= 0:
        return args_err("--run-threshold must be > 0.")
    if args.drawdown_threshold <= 0:
        return args_err("--drawdown-threshold must be > 0.")
    if args.flat_gap_hours <= 0:
        return args_err("--flat-gap-hours must be > 0.")
    if args.flat_band_pct <= 0:
        return args_err("--flat-band-pct must be > 0.")
    if args.flat_band_pct >= args.run_threshold:
        return args_err(
            "--flat-band-pct must be strictly less than --run-threshold "
            "(a 'flat' band must be narrower than a 'run')."
        )
    if args.min_points < 2:
        return args_err("--min-points must be >= 2.")
    if maturity_hours <= 0:
        return args_err("--maturity-hours must be > 0.")
    if args.lookback_days < 1:
        return args_err("--lookback-days must be >= 1.")

    now = datetime.now(timezone.utc)

    try:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return _err(
            {"status": "error", "stage": "db_open", "error": str(exc)}, args.json
        )

    try:
        try:
            cohort_rows = _build_cohort(conn, args.cohort, args.lookback_days, now)
        except sqlite3.Error as exc:
            return _err(
                {"status": "error", "stage": "cohort", "error": str(exc)}, args.json
            )

        report = build_report(
            cohort_rows,
            conn,
            window_hours=args.window_hours,
            run_threshold=args.run_threshold,
            drawdown_threshold=args.drawdown_threshold,
            flat_gap_hours=args.flat_gap_hours,
            flat_band_pct=args.flat_band_pct,
            min_points=args.min_points,
            maturity_hours=maturity_hours,
            now=now,
            sensitivity=args.sensitivity,
            cohort=args.cohort,
            lookback_days=args.lookback_days,
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
