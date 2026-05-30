#!/usr/bin/env python3
"""Signal early tradable usefulness scorecard (offline, read-only audit).

Evaluates each signal family (``paper_trades.signal_type``) on *early tradable
usefulness* — not eventual paper PnL. For each detected candidate the audit
reconstructs the post-detection price path from ``volume_history_cg`` and asks:
did the signal surface an inspectable candidate before the move, with enough
tradability data to act?

This is a purely DB-local diagnostic. Unlike ``audit_price_path_coverage.py``
(which fetches the live ``/api/todays_focus`` endpoint), this audit makes NO
network calls — the metric-5 surface-timing fact is derived from the only
persisted proxy (``gainers_comparisons.appeared_on_gainers_at``) and is
``unsupported_for_signal`` for families without a persisted surface ts.

Output is descriptive statistics per signal family only: no ranking, no
enable/disable verdict, no alert intent, no threshold tuning. See the design
``tasks/design_signal_early_usefulness_scorecard_2026_05_29.md`` for the
verified schema (every column cited against ``scout/db.py``) and the pinned
decisions.

P0 / entry basis is ``paper_trades.entry_price`` (REAL NOT NULL) for EVERY
signal, making MFE/MAE cross-signal comparable and comparable to paper PnL.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

HORIZON_HOURS_CEILING = 168  # 7d volume_history_cg writer retention ceiling.
RETENTION_DAYS = 7

INFINITY_GUARD_MAX = 1e308  # defensive ceiling against +Inf in REAL columns.

ALTERNATE_PRICE_HISTORY_TABLES = (
    "gainers_snapshots",
    "losers_snapshots",
    "momentum_7d",
    "slow_burn_candidates",
    "volume_spikes",
)

VENUE_ROUTE_UNSUPPORTED_REASON = (
    "no venue column on paper_trades or paper_trade_entry_snapshots; "
    "venue_* tables are the BL-055 live layer keyed by (venue,symbol)"
)

COMPARABILITY_WARNING = "MFE/MAE comparable only within similar join-rate bands"

# REQUIRED schema (fold round 2): absence of any of these forces a stage="schema"
# exit 2 in main(), NOT a silent empty cohort / INSUFFICIENT_DATA. OPTIONAL-cohort
# tables (gainers_comparisons, paper_trade_entry_snapshots) degrade gracefully via
# schema_findings flags — they never force exit 2.
REQUIRED_PRICE_PATH_COLUMNS = ("coin_id", "price", "recorded_at")
REQUIRED_PAPER_TRADES_COLUMNS = ("token_id", "signal_type", "opened_at", "entry_price")


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------


def _utc_iso_z(now: datetime) -> str:
    return now.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------
# Schema probes (mirror reference; swallow sqlite3.Error -> False)
# --------------------------------------------------------------------------


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


# --------------------------------------------------------------------------
# Distribution + rate helpers
# --------------------------------------------------------------------------


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    idx = max(0, min(n - 1, int(round(q * (n - 1)))))
    return sorted_values[idx]


def _float_distribution(
    values: list[float], *, min_samples: int
) -> dict[str, float] | None:
    """Forked float-typed distribution with a configurable floor.

    The reference ``_points_distribution`` is ``int``-typed and hardcodes a
    floor of 5; this audit needs float MFE/MAE values and a configurable floor
    (``min_n_dist``, default 10). Returns ``None`` when ``len(values) <
    min_samples`` so small-N groups never emit false percentiles.
    """
    if len(values) < min_samples:
        return None
    s = sorted(float(v) for v in values)
    return {
        "min": s[0],
        "p25": _quantile(s, 0.25),
        "p50": _quantile(s, 0.50),
        "p75": _quantile(s, 0.75),
        "p90": _quantile(s, 0.90),
        "max": s[-1],
        "mean": round(sum(s) / len(s), 6),
    }


def _rate_or_null(num: int, denom: int) -> float | None:
    if denom <= 0:
        return None
    return round(num / denom, 4)


def _gated_rate(
    num: int, denom: int, *, min_n_dist: int, immature_excluded: int
) -> dict[str, Any]:
    """A rate that self-reports its denominator and suppresses on small mature n.

    Fold round 2 (Codex IMPORTANT + statistical I2): a rate gated only on overall
    n_joinable can print a confident number from a handful of mature rows. This
    helper carries ``n`` (the mature denominator), an ``immature_excluded``
    count, and a ``low_confidence`` flag; the ``rate`` is suppressed to None when
    ``n < min_n_dist`` so a tiny mature sample never emits a confident rate.
    """
    low_confidence = denom < min_n_dist
    return {
        "rate": None if low_confidence else _rate_or_null(num, denom),
        "n": denom,
        "favorable_n": num,
        "immature_excluded": immature_excluded,
        "low_confidence": low_confidence,
    }


# --------------------------------------------------------------------------
# Cohort + price-path loading
# --------------------------------------------------------------------------


def _load_cohort(conn: sqlite3.Connection, cutoff_iso: str) -> list[dict[str, Any]]:
    """Load detection rows from paper_trades within the lookback window.

    Raises sqlite3.Error if paper_trades is missing/unreadable (surfaced as a
    ``stage:"query"`` failure by main()).
    """
    cursor = conn.execute(
        "SELECT id, token_id, symbol, signal_type, opened_at, chain, entry_price, "
        "actionable, actionability_reason "
        "FROM paper_trades WHERE opened_at >= ? ORDER BY opened_at ASC",
        (cutoff_iso,),
    )
    rows = []
    for r in cursor.fetchall():
        rows.append(
            {
                "id": r[0],
                "token_id": r[1] or "",
                "symbol": r[2] or "",
                "signal_type": r[3] or "",
                "opened_at": r[4] or "",
                "chain": r[5] or "",
                "entry_price": r[6],
                "actionable": r[7],
                "actionability_reason": r[8],
            }
        )
    return rows


def _load_price_path(
    conn: sqlite3.Connection, token_id: str, t0_iso: str, cutoff_hi_iso: str
) -> list[tuple[float, datetime]]:
    """Return [(price, recorded_at)] within [t0, cutoff_hi], price-guarded.

    Fold round 2 (Codex CRITICAL): do NOT swallow sqlite3.Error into ``[]`` here.
    A missing/renamed volume_history_cg table or any genuine query-time failure
    would otherwise masquerade as an unjoinable row with exit 0 — a silent
    failure indistinguishable from "schema OK but this token has no in-window
    points." The schema precondition in main() guarantees the table + required
    columns exist before we get here; any residual query error is allowed to
    propagate so the top-level handler maps it to stage="query" / exit 2.
    An empty path now means STRICTLY "schema OK but no joinable points."
    """
    if not token_id:
        return []
    cursor = conn.execute(
        "SELECT price, recorded_at FROM volume_history_cg "
        "WHERE coin_id = ? AND recorded_at >= ? AND recorded_at <= ? "
        "AND price IS NOT NULL AND price > 0 AND price < ? "
        "ORDER BY recorded_at ASC",
        (token_id, t0_iso, cutoff_hi_iso, INFINITY_GUARD_MAX),
    )
    path = []
    for price, recorded_at in cursor.fetchall():
        ts = _parse_iso(recorded_at)
        if ts is None:
            continue
        path.append((float(price), ts))
    return path


def _gainers_metric5_schema_ok(conn: sqlite3.Connection) -> bool:
    """True iff the metric-5 surface-timing column path is queryable.

    Fold round 2 (Codex IMPORTANT + statistical I): if gainers_comparisons exists
    but lacks ``appeared_on_gainers_at``, the surface-timing fact is unsupported.
    We must NOT silently bucket every gainers row as ``not_surfaced`` (a
    misleading semantic value); instead we record a ``metric5_schema_unavailable``
    flag and emit ``unsupported_for_signal``.
    """
    return _table_exists(conn, "gainers_comparisons") and _column_exists(
        conn, "gainers_comparisons", "appeared_on_gainers_at"
    )


def _price_path_has_any_row(conn: sqlite3.Connection) -> bool:
    """True iff volume_history_cg holds at least one valid price row.

    Fold round 2 / statistical I1: lets the report assert
    ``price_path_source_available`` so all-zero joins are self-explaining (table
    present but empty) rather than read as a per-signal fact. Not swallowed:
    callers run after the schema precondition guarantees the table/columns exist.
    """
    cursor = conn.execute(
        "SELECT 1 FROM volume_history_cg "
        "WHERE price IS NOT NULL AND price > 0 LIMIT 1"
    )
    return cursor.fetchone() is not None


def _load_gainer_surface_ts(conn: sqlite3.Connection, token_id: str) -> datetime | None:
    """Earliest appeared_on_gainers_at for the token, or None if no such row.

    Fold round 2: no longer swallows sqlite3.Error. Callers guard via
    ``_gainers_metric5_schema_ok`` so the needed column is known to exist before
    this is called; a residual query error propagates to the stage="query"
    handler in main(). A None return now means STRICTLY "schema OK but this
    token has no surface timestamp," never "column missing."
    """
    if not token_id:
        return None
    cursor = conn.execute(
        "SELECT appeared_on_gainers_at FROM gainers_comparisons "
        "WHERE coin_id = ? ORDER BY appeared_on_gainers_at ASC LIMIT 1",
        (token_id,),
    )
    row = cursor.fetchone()
    if not row or row[0] is None:
        return None
    return _parse_iso(row[0])


SNAPSHOT_FACT_COLUMNS = (
    "liquidity_usd_at_entry",
    "actionability_reason_at_entry",
    "actionable_at_entry",
)


def _snapshot_schema_ok(conn: sqlite3.Connection) -> bool:
    """True iff paper_trade_entry_snapshots is present with all fact columns.

    Fold round 2 (Codex IMPORTANT): a snapshot-table schema drift (table present
    but a fact column renamed/dropped) must NOT silently produce false
    fresh_price / liquidity facts. When this returns False, the at-detection
    facts collapse to None (schema-unavailable), surfaced via the
    ``snapshot_facts_schema_unavailable`` flag in schema_findings.
    """
    if not _table_exists(conn, "paper_trade_entry_snapshots"):
        return False
    return all(
        _column_exists(conn, "paper_trade_entry_snapshots", col)
        for col in SNAPSHOT_FACT_COLUMNS
    )


def _load_snapshot(
    conn: sqlite3.Connection, paper_trade_id: int
) -> dict[str, Any] | None:
    """Return the entry-snapshot row for a paper trade, or None if absent.

    Fold round 2: no longer swallows sqlite3.Error. Callers guard via
    ``_snapshot_schema_ok`` so the table + fact columns are known to exist; a
    residual query error propagates to the stage="query" handler in main().
    """
    cursor = conn.execute(
        "SELECT liquidity_usd_at_entry, actionability_reason_at_entry, "
        "actionable_at_entry FROM paper_trade_entry_snapshots "
        "WHERE paper_trade_id = ? LIMIT 1",
        (paper_trade_id,),
    )
    row = cursor.fetchone()
    if not row:
        return None
    return {
        "liquidity_usd_at_entry": row[0],
        "actionability_reason_at_entry": row[1],
        "actionable_at_entry": row[2],
    }


# --------------------------------------------------------------------------
# Per-row metric computation
# --------------------------------------------------------------------------


def _corpus_for_chain(chain: str) -> str:
    return "cg-watcher" if chain == "coingecko" else "micro-cap"


def _derive_corpus(rows: list[dict[str, Any]]) -> str:
    corpora = {_corpus_for_chain(r["chain"]) for r in rows}
    if len(corpora) == 1:
        return next(iter(corpora))
    return "mixed"


def _compute_row(
    conn: sqlite3.Connection,
    row: dict[str, Any],
    *,
    horizons_h: list[int],
    fav_eps: float,
    now: datetime,
    snapshots_present: bool,
    metric5_supported: bool,
) -> dict[str, Any]:
    """Compute per-row usefulness facts for a single (deduped) detection."""
    t0 = _parse_iso(row["opened_at"])
    p0 = row["entry_price"]
    max_h = max(horizons_h)

    result: dict[str, Any] = {
        "joinable": False,
        "per_horizon": {},  # h -> {mfe, mature, window_elapsed_fraction, in_window}
        "time_to_peak_minutes": None,
        "peak_at_window_edge": None,
        "max_horizon_mature": None,
        "mae_before_favorable": None,
        "favorable_reached": None,
        "appeared_on_gainers_timing": None,
    }

    if t0 is None or p0 is None or p0 <= 0:
        # Cannot compute moves; treat as unjoinable for usefulness purposes.
        result["max_horizon_mature"] = False
        for h in horizons_h:
            result["per_horizon"][h] = {
                "mfe": None,
                "mature": (t0 is not None) and (t0 + timedelta(hours=h) <= now),
                "window_elapsed_fraction": 0.0,
                "in_window": False,
            }
        result.update(
            _at_detection_facts(conn, row, snapshots_present=snapshots_present)
        )
        return result

    cutoff_hi = min(t0 + timedelta(hours=max_h), now)
    path = _load_price_path(
        conn, row["token_id"], t0.isoformat(), cutoff_hi.isoformat()
    )
    result["joinable"] = len(path) > 0

    def ret(price: float) -> float:
        return (price - p0) / p0

    # ---- Per-horizon MFE ----
    for h in horizons_h:
        h_edge = t0 + timedelta(hours=h)
        mature = h_edge <= now
        elapsed = (now - t0).total_seconds() / (h * 3600.0)
        window_elapsed_fraction = max(0.0, min(1.0, elapsed))
        win = [p for (p, ts) in path if ts <= h_edge]
        mfe = max((ret(p) for p in win), default=None)
        result["per_horizon"][h] = {
            "mfe": mfe,
            "mature": mature,
            "window_elapsed_fraction": round(window_elapsed_fraction, 6),
            "in_window": len(win) > 0,
        }

    # ---- Max-horizon-gated metrics: time-to-peak, peak-edge, MAE ----
    max_edge = t0 + timedelta(hours=max_h)
    max_horizon_mature = max_edge <= now
    result["max_horizon_mature"] = max_horizon_mature

    window = [(p, ts) for (p, ts) in path if ts <= max_edge]
    if window:
        peak_price, peak_ts = max(window, key=lambda pt: pt[0])
        result["time_to_peak_minutes"] = (peak_ts - t0).total_seconds() / 60.0
        is_last = peak_ts == window[-1][1]
        result["peak_at_window_edge"] = bool(is_last and not max_horizon_mature)

        # MAE before favorable over the full observed window.
        favorable_idx = None
        for i, (p, _ts) in enumerate(window):
            if ret(p) > fav_eps:
                favorable_idx = i
                break
        if favorable_idx is None:
            result["mae_before_favorable"] = min(ret(p) for (p, _ts) in window)
            result["favorable_reached"] = False
        else:
            pre = window[:favorable_idx]
            result["mae_before_favorable"] = (
                min(ret(p) for (p, _ts) in pre) if pre else 0.0
            )
            result["favorable_reached"] = True

    # ---- Metric 5 (gainers cohort only) ----
    if metric5_supported:
        surf_ts = _load_gainer_surface_ts(conn, row["token_id"])
        if surf_ts is None:
            result["appeared_on_gainers_timing"] = "not_surfaced"
        elif not window:
            result["appeared_on_gainers_timing"] = "surfaced_no_observed_move"
        else:
            _peak_price, peak_ts = max(window, key=lambda pt: pt[0])
            if surf_ts <= peak_ts:
                result["appeared_on_gainers_timing"] = "before_peak"
            else:
                result["appeared_on_gainers_timing"] = "after_peak"

    result.update(_at_detection_facts(conn, row, snapshots_present=snapshots_present))
    return result


def _entry_snapshot_coverage_rate(
    conn: sqlite3.Connection,
    rows: list[dict[str, Any]],
    *,
    snapshots_present: bool,
) -> float | None:
    """Fraction of cohort rows that have an entry-snapshot row.

    Fold round 2 / statistical I3: this is the data-path coverage denominator.
    A low ``fresh_price_rate`` could mean either "detection genuinely lacked a
    fresh price" or "the snapshot writer never wrote a row." Surfacing coverage
    separately lets a reader attribute the gap correctly. None when the snapshot
    schema is unavailable (there is nothing to cover).
    """
    if not snapshots_present:
        return None
    if not rows:
        return None
    covered = sum(1 for r in rows if _load_snapshot(conn, r["id"]) is not None)
    return _rate_or_null(covered, len(rows))


def _at_detection_facts(
    conn: sqlite3.Connection, row: dict[str, Any], *, snapshots_present: bool
) -> dict[str, Any]:
    """Tri-state at-detection fact flags (True / False / None-schema-absent)."""
    facts: dict[str, Any] = {
        "had_entry_snapshot": None,
        "had_fresh_price_at_detection": None,
        "had_venue_route_at_detection": None,  # permanently None (no column).
        "had_liquidity_fact_at_detection": None,
        "actionability_state_at_detection": None,
    }
    if not snapshots_present:
        # Table absent: cohort-neutral flags collapse to None, never False.
        return facts

    snap = _load_snapshot(conn, row["id"])
    has_snap = snap is not None
    facts["had_entry_snapshot"] = has_snap
    # Cohort-neutral fresh-price = snapshot presence (a price was observed at
    # entry). NOT read from gainers_comparisons.detected_price for non-gainers.
    facts["had_fresh_price_at_detection"] = has_snap
    if has_snap:
        facts["had_liquidity_fact_at_detection"] = (
            snap["liquidity_usd_at_entry"] is not None
        )
        if snap["actionable_at_entry"] is not None:
            facts["actionability_state_at_detection"] = bool(
                snap["actionable_at_entry"]
            )
        elif row["actionable"] is not None:
            facts["actionability_state_at_detection"] = bool(row["actionable"])
    else:
        facts["had_liquidity_fact_at_detection"] = False
        if row["actionable"] is not None:
            facts["actionability_state_at_detection"] = bool(row["actionable"])
    return facts


# --------------------------------------------------------------------------
# Dedup
# --------------------------------------------------------------------------


def _dedup_earliest(
    rows: list[dict[str, Any]], dedup: bool
) -> tuple[list[dict[str, Any]], int]:
    """Collapse to earliest opened_at per (token_id, signal_type) if dedup.

    Returns (surviving_rows, multi_fire_collapsed_count). The multi-fire count
    is the number of rows that were collapsed away (reported either way).
    """
    by_key: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        by_key.setdefault((r["token_id"], r["signal_type"]), []).append(r)

    multi_fire = sum(len(v) - 1 for v in by_key.values() if len(v) > 1)

    if not dedup:
        return rows, multi_fire

    survivors = []
    for group in by_key.values():
        earliest = min(group, key=lambda r: r["opened_at"])
        survivors.append(earliest)
    survivors.sort(key=lambda r: r["opened_at"])
    return survivors, multi_fire


# --------------------------------------------------------------------------
# Aggregation
# --------------------------------------------------------------------------


def _aggregate_signal(
    conn: sqlite3.Connection,
    signal_type: str,
    rows: list[dict[str, Any]],
    *,
    horizons_h: list[int],
    min_n: int,
    min_n_dist: int,
    fav_eps: float,
    now: datetime,
    snapshots_present: bool,
    metric5_schema_ok: bool,
) -> dict[str, Any]:
    corpus = _derive_corpus(rows)
    n_total = len(rows)

    # Fold round 2: metric-5 support requires BOTH the schema (table + column)
    # AND at least one row that actually joins gainers_comparisons. If the schema
    # is unavailable, metric5 is unsupported regardless of cohort — we never
    # probe rows against a missing column.
    metric5_supported = metric5_schema_ok and any(
        _load_gainer_surface_ts(conn, r["token_id"]) is not None
        or _gainers_has_row(conn, r["token_id"])
        for r in rows
    )

    computed = [
        _compute_row(
            conn,
            r,
            horizons_h=horizons_h,
            fav_eps=fav_eps,
            now=now,
            snapshots_present=snapshots_present,
            metric5_supported=metric5_supported,
        )
        for r in rows
    ]

    n_joinable = sum(1 for c in computed if c["joinable"])
    n_unjoinable = n_total - n_joinable

    base = {
        "corpus": corpus,
        "n_total": n_total,
        "n_joinable": n_joinable,
        "n_unjoinable": n_unjoinable,
        "metric5_data_path_available": metric5_supported,
        # Fold round 2: True when gainers_comparisons exists but lacks the
        # appeared_on_gainers_at column. Distinguishes "schema drift, fact
        # unsupported" from "schema OK, token simply not_surfaced."
        "metric5_schema_unavailable": (not metric5_schema_ok),
        # Fold round 2 / statistical I3: entry-snapshot coverage as its OWN
        # per-signal metric, so a low fresh-price rate attributable to MISSING
        # snapshot writes (a data-path gap) is visible separately from a real
        # detection-freshness fact. None when the snapshot schema is unavailable.
        "entry_snapshot_coverage_rate": _entry_snapshot_coverage_rate(
            conn, rows, snapshots_present=snapshots_present
        ),
    }

    if n_joinable < min_n:
        base["status"] = "INSUFFICIENT_DATA"
        base["min_n"] = min_n
        return base

    metrics = _build_metrics(
        computed,
        horizons_h=horizons_h,
        min_n_dist=min_n_dist,
        metric5_supported=metric5_supported,
    )
    return {
        "status": "OK",
        **base,
        "multi_fire_rows": 0,  # patched in by caller (dedup-aware)
        "comparability_warning": COMPARABILITY_WARNING,
        "metrics": metrics,
    }


def _gainers_has_row(conn: sqlite3.Connection, token_id: str) -> bool:
    """True iff the token has any gainers_comparisons row.

    Fold round 2: no longer swallows sqlite3.Error. Callers guard via
    ``_gainers_metric5_schema_ok`` (which confirms the table exists) before
    invoking; a residual query error propagates to the stage="query" handler.
    """
    if not token_id:
        return False
    cursor = conn.execute(
        "SELECT 1 FROM gainers_comparisons WHERE coin_id = ? LIMIT 1",
        (token_id,),
    )
    return cursor.fetchone() is not None


def _build_metrics(
    computed: list[dict[str, Any]],
    *,
    horizons_h: list[int],
    min_n_dist: int,
    metric5_supported: bool,
) -> dict[str, Any]:
    joinable = [c for c in computed if c["joinable"]]

    # ---- Time-to-peak + peak-edge + MAE: gated on max-horizon maturity ----
    mature_max = [c for c in joinable if c["max_horizon_mature"]]
    immature_max = [c for c in joinable if not c["max_horizon_mature"]]

    ttp_values = [
        c["time_to_peak_minutes"]
        for c in mature_max
        if c["time_to_peak_minutes"] is not None
    ]
    # Peak-at-window-edge is the edge-censoring flag; it is meaningful precisely
    # for immature windows, so it is computed over ALL joinable rows (not gated
    # to mature rows like the time-to-peak distribution).
    edge_flags = [
        c["peak_at_window_edge"]
        for c in joinable
        if c["peak_at_window_edge"] is not None
    ]
    mae_rows = [c for c in mature_max if c["mae_before_favorable"] is not None]
    mae_values = [c["mae_before_favorable"] for c in mae_rows]
    favorable_flags = [
        c["favorable_reached"] for c in mature_max if c["favorable_reached"] is not None
    ]

    metrics: dict[str, Any] = {
        "time_to_peak_within_max_horizon_minutes": _float_distribution(
            ttp_values, min_samples=min_n_dist
        ),
        "time_to_peak_immature_excluded": len(immature_max),
        "peak_at_window_edge_rate": _rate_or_null(
            sum(1 for f in edge_flags if f), len(edge_flags)
        ),
        "mae_before_favorable": {
            "n": len(mae_values),
            "immature_excluded": len(immature_max),
            "low_confidence": len(mae_values) < min_n_dist,
            "window_elapsed_fraction": 1.0,
            "dist": _float_distribution(mae_values, min_samples=min_n_dist),
        },
        "mae_immature_excluded": len(immature_max),
        # Fold round 2 (Codex IMPORTANT + statistical I2): favorable_reached_rate
        # is gated on the MATURE max-horizon n (favorable_flags already excludes
        # immature rows) and carries its denominator + a low_confidence flag when
        # that mature n is small. A bare confident rate from a tiny mature sample
        # is suppressed (rate=None) so the reader is not misled.
        "favorable_reached_rate": _gated_rate(
            sum(1 for f in favorable_flags if f),
            len(favorable_flags),
            min_n_dist=min_n_dist,
            immature_excluded=len(immature_max),
        ),
    }

    # ---- Per-horizon MFE ----
    for h in horizons_h:
        mature_vals = [
            c["per_horizon"][h]["mfe"]
            for c in joinable
            if c["per_horizon"][h]["mature"] and c["per_horizon"][h]["mfe"] is not None
        ]
        immature_excluded = sum(
            1 for c in joinable if not c["per_horizon"][h]["mature"]
        )
        # window_elapsed_fraction: representative (max over rows) for the block.
        fracs = [c["per_horizon"][h]["window_elapsed_fraction"] for c in joinable]
        metrics[f"mfe_{h}h"] = {
            "n": len(mature_vals),
            "immature_excluded": immature_excluded,
            "low_confidence": len(mature_vals) < min_n_dist,
            "window_elapsed_fraction": round(max(fracs), 6) if fracs else 0.0,
            "dist": _float_distribution(mature_vals, min_samples=min_n_dist),
        }

    # ---- At-detection facts (fraction True over non-None) ----
    metrics["at_detection_facts"] = _aggregate_facts(computed)

    # ---- Metric 5 ----
    if metric5_supported:
        buckets: dict[str, Any] = {
            "before_peak": 0,
            "after_peak": 0,
            "surfaced_no_observed_move": 0,
            "not_surfaced": 0,
            # Fold round 2 (NIT): make the denominator explicit. not_surfaced
            # counts ALL cohort rows (incl. unjoinable) without a surface ts, so
            # the bucket totals are over n_total, not n_joinable.
            "_denominator": "n_total (incl. unjoinable)",
        }
        for c in computed:
            timing = c["appeared_on_gainers_timing"]
            if timing in buckets and timing != "_denominator":
                buckets[timing] += 1
        metrics["appeared_on_gainers_timing"] = buckets
    else:
        metrics["appeared_on_gainers_timing"] = "unsupported_for_signal"

    return metrics


def _aggregate_facts(computed: list[dict[str, Any]]) -> dict[str, Any]:
    def rate(key: str) -> float | None:
        vals = [c[key] for c in computed if c[key] is not None]
        if not vals:
            return None
        return _rate_or_null(sum(1 for v in vals if v), len(vals))

    return {
        "fresh_price_rate": rate("had_fresh_price_at_detection"),
        "venue_route_rate": None,  # permanently None (no venue column exists).
        "liquidity_fact_rate": rate("had_liquidity_fact_at_detection"),
        "actionable_rate": rate("actionability_state_at_detection"),
    }


# --------------------------------------------------------------------------
# schema_findings
# --------------------------------------------------------------------------


def _schema_precondition_error(conn: sqlite3.Connection) -> str | None:
    """Return a stage="schema" error string, or None if REQUIRED schema is OK.

    Fold round 2 (Codex CRITICAL x2): REQUIRED schema = the price-path table
    (volume_history_cg) + its coin_id/price/recorded_at columns, AND the cohort
    table (paper_trades) + its entry_price/token_id/signal_type/opened_at
    columns. A missing REQUIRED table/column forces exit 2 rather than an empty
    cohort / all-unjoinable report with exit 0. OPTIONAL-cohort tables
    (gainers_comparisons, paper_trade_entry_snapshots) are NOT checked here —
    they degrade gracefully via schema_findings.
    """
    if not _table_exists(conn, "volume_history_cg"):
        return "required table 'volume_history_cg' is missing."
    for col in REQUIRED_PRICE_PATH_COLUMNS:
        if not _column_exists(conn, "volume_history_cg", col):
            return f"required column 'volume_history_cg.{col}' is missing."
    if not _table_exists(conn, "paper_trades"):
        return "required table 'paper_trades' is missing."
    for col in REQUIRED_PAPER_TRADES_COLUMNS:
        if not _column_exists(conn, "paper_trades", col):
            return f"required column 'paper_trades.{col}' is missing."
    return None


def _schema_findings(conn: sqlite3.Connection, *, lookback_days: int) -> dict[str, Any]:
    return {
        "paper_trades_has_signal_type": _column_exists(
            conn, "paper_trades", "signal_type"
        ),
        "paper_trades_has_opened_at": _column_exists(conn, "paper_trades", "opened_at"),
        "paper_trades_has_token_id": _column_exists(conn, "paper_trades", "token_id"),
        "paper_trades_has_entry_price": _column_exists(
            conn, "paper_trades", "entry_price"
        ),
        "volume_history_cg_has_price": _column_exists(
            conn, "volume_history_cg", "price"
        ),
        "volume_history_cg_has_recorded_at": _column_exists(
            conn, "volume_history_cg", "recorded_at"
        ),
        "gainers_comparisons_present": _table_exists(conn, "gainers_comparisons"),
        "gainers_comparisons_has_appeared_on_gainers_at": _column_exists(
            conn, "gainers_comparisons", "appeared_on_gainers_at"
        ),
        "gainers_comparisons_has_detected_price": _column_exists(
            conn, "gainers_comparisons", "detected_price"
        ),
        "paper_trade_entry_snapshots_present": _table_exists(
            conn, "paper_trade_entry_snapshots"
        ),
        "ptes_has_actionable_at_entry": _column_exists(
            conn, "paper_trade_entry_snapshots", "actionable_at_entry"
        ),
        "ptes_has_actionability_reason_at_entry": _column_exists(
            conn, "paper_trade_entry_snapshots", "actionability_reason_at_entry"
        ),
        "ptes_has_liquidity_usd_at_entry": _column_exists(
            conn, "paper_trade_entry_snapshots", "liquidity_usd_at_entry"
        ),
        # Fold round 2: explicit schema-drift flags for the OPTIONAL-cohort
        # tables. True means the table exists but a needed column is absent, so
        # the corresponding fact degrades to unsupported/None rather than a
        # misleading semantic value (not_surfaced / false fresh_price).
        "metric5_schema_unavailable": (
            _table_exists(conn, "gainers_comparisons")
            and not _column_exists(
                conn, "gainers_comparisons", "appeared_on_gainers_at"
            )
        ),
        "snapshot_facts_schema_unavailable": (
            _table_exists(conn, "paper_trade_entry_snapshots")
            and not all(
                _column_exists(conn, "paper_trade_entry_snapshots", col)
                for col in SNAPSHOT_FACT_COLUMNS
            )
        ),
        "venue_route_unsupported_reason": VENUE_ROUTE_UNSUPPORTED_REASON,
        "alternate_price_history_tables_present": {
            name: _table_exists(conn, name) for name in ALTERNATE_PRICE_HISTORY_TABLES
        },
        "lookback_exceeds_retention": lookback_days > RETENTION_DAYS,
    }


# --------------------------------------------------------------------------
# Pure core
# --------------------------------------------------------------------------


def build_report(
    conn: sqlite3.Connection,
    horizons_h: list[int],
    min_n: int,
    min_n_dist: int,
    fav_eps: float,
    lookback_days: int,
    dedup: bool,
    now: datetime,
) -> dict[str, Any]:
    cutoff = now - timedelta(days=lookback_days)
    cutoff_iso = cutoff.isoformat()

    # The cohort load reads paper_trades columns directly. If the table exists
    # but a required column is absent (schema drift / migration not applied),
    # degrade gracefully: emit schema_findings with the False flag and an empty
    # cohort rather than crashing. A genuinely-missing paper_trades table still
    # raises sqlite3.Error -> surfaced as a stage:"query" failure by main().
    if _table_exists(conn, "paper_trades") and not all(
        _column_exists(conn, "paper_trades", col)
        for col in REQUIRED_PAPER_TRADES_COLUMNS
    ):
        cohort: list[dict[str, Any]] = []
    else:
        cohort = _load_cohort(conn, cutoff_iso)
    # Fold round 2: snapshot "presence" now means the FULL fact schema is
    # queryable (table + all fact columns), so a column-renamed table degrades to
    # None facts instead of producing false fresh_price / liquidity values.
    snapshots_present = _snapshot_schema_ok(conn)
    metric5_schema_ok = _gainers_metric5_schema_ok(conn)
    # Fold round 2 / statistical I1: belt-and-suspenders flag so a reader of the
    # all-zero-join per-signal blocks is not misled when volume_history_cg exists
    # but is EMPTY (the schema precondition only proves the table/columns exist).
    price_path_source_available = (
        _table_exists(conn, "volume_history_cg")
        and _column_exists(conn, "volume_history_cg", "price")
        and _price_path_has_any_row(conn)
    )

    # Group by signal_type (never dedup across signals).
    by_signal: dict[str, list[dict[str, Any]]] = {}
    for r in cohort:
        by_signal.setdefault(r["signal_type"], []).append(r)

    signals: dict[str, Any] = {}
    for signal_type, rows in by_signal.items():
        survivors, multi_fire = _dedup_earliest(rows, dedup)
        block = _aggregate_signal(
            conn,
            signal_type,
            survivors,
            horizons_h=horizons_h,
            min_n=min_n,
            min_n_dist=min_n_dist,
            fav_eps=fav_eps,
            now=now,
            snapshots_present=snapshots_present,
            metric5_schema_ok=metric5_schema_ok,
        )
        # Fold round 2 (NIT): collapse the dead if/else that set multi_fire_rows
        # identically in both branches.
        block["multi_fire_rows"] = multi_fire
        signals[signal_type] = block

    return {
        "audited_at": _utc_iso_z(now),
        "price_path_source_available": price_path_source_available,
        "params": {
            "horizons_h": horizons_h,
            "min_n": min_n,
            "min_n_dist": min_n_dist,
            "fav_eps": fav_eps,
            "lookback_days": lookback_days,
            "dedup": dedup,
            "cohort_cutoff_iso": cutoff_iso,
            "now_iso": now.isoformat(),
        },
        "total_rows": len(cohort),
        "signals": signals,
        "schema_findings": _schema_findings(conn, lookback_days=lookback_days),
    }


# --------------------------------------------------------------------------
# Human formatter
# --------------------------------------------------------------------------


def _format_human(report: dict[str, Any]) -> str:
    p = report["params"]
    lines = [
        f"audited_at:    {report['audited_at']}",
        f"price_path_source_available: {report['price_path_source_available']}",
        f"horizons_h:    {p['horizons_h']}",
        f"min_n:         {p['min_n']}  min_n_dist: {p['min_n_dist']}",
        f"fav_eps:       {p['fav_eps']}  dedup: {p['dedup']}",
        f"lookback_days: {p['lookback_days']}  cutoff: {p['cohort_cutoff_iso']}",
        f"total_rows:    {report['total_rows']}",
        "",
        "SIGNALS:",
    ]
    for signal_type, block in report["signals"].items():
        lines.append(f"  [{signal_type}] status={block['status']}")
        lines.append(
            f"    corpus={block['corpus']} n_total={block['n_total']} "
            f"n_joinable={block['n_joinable']} n_unjoinable={block['n_unjoinable']} "
            f"multi_fire_rows={block.get('multi_fire_rows')}"
        )
        lines.append(
            f"    metric5_data_path_available="
            f"{block['metric5_data_path_available']} "
            f"metric5_schema_unavailable={block.get('metric5_schema_unavailable')}"
        )
        lines.append(
            f"    entry_snapshot_coverage_rate="
            f"{block.get('entry_snapshot_coverage_rate')}"
        )
        if block["status"] != "OK":
            continue
        metrics = block["metrics"]
        ttp = metrics["time_to_peak_within_max_horizon_minutes"]
        lines.append(f"    time_to_peak_min        = {ttp}")
        lines.append(
            f"    peak_at_window_edge_rate= {metrics['peak_at_window_edge_rate']}"
        )
        for key in sorted(k for k in metrics if k.startswith("mfe_")):
            blk = metrics[key]
            marker = " LOW_CONFIDENCE" if blk["low_confidence"] else ""
            lines.append(
                f"    {key:<22}= n={blk['n']} immature={blk['immature_excluded']}"
                f"{marker} dist={blk['dist']}"
            )
        mae = metrics["mae_before_favorable"]
        mae_marker = " LOW_CONFIDENCE" if mae["low_confidence"] else ""
        lines.append(
            f"    mae_before_favorable  = n={mae['n']}{mae_marker} dist={mae['dist']}"
        )
        fav = metrics["favorable_reached_rate"]
        fav_marker = " LOW_CONFIDENCE" if fav["low_confidence"] else ""
        lines.append(
            f"    favorable_reached_rate= rate={fav['rate']} n={fav['n']}"
            f"{fav_marker}"
        )
        lines.append(f"    at_detection_facts    = {metrics['at_detection_facts']}")
        lines.append(
            f"    appeared_on_gainers   = {metrics['appeared_on_gainers_timing']}"
        )
    lines.append("")
    lines.append("SCHEMA FINDINGS:")
    for key, value in report["schema_findings"].items():
        lines.append(f"  {key} = {value}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def _parse_horizons(raw: str) -> list[int]:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        raise ValueError("--horizons must contain at least one positive integer")
    out = set()
    for part in parts:
        try:
            h = int(part)
        except ValueError as exc:
            raise ValueError(f"--horizons entry {part!r} is not an integer") from exc
        if h < 1 or h > HORIZON_HOURS_CEILING:
            raise ValueError(
                f"--horizons entry {h} must be in [1, {HORIZON_HOURS_CEILING}]"
            )
        out.add(h)
    return sorted(out)


def _emit_error(stage: str, error: str, as_json: bool) -> int:
    msg = {"status": "error", "stage": stage, "error": error}
    if as_json:
        print(json.dumps(msg))
    else:
        print(f"ERROR ({stage}): {error}", file=sys.stderr)
    return 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default="scout.db")
    parser.add_argument("--horizons", default="1,4,24")
    parser.add_argument("--min-n", type=int, default=5)
    parser.add_argument("--min-n-dist", type=int, default=10)
    parser.add_argument("--fav-eps", type=float, default=0.01)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--no-dedup", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    # ---- Argument validation (stage: args) ----
    try:
        horizons_h = _parse_horizons(args.horizons)
    except ValueError as exc:
        return _emit_error("args", str(exc), args.json)

    if args.min_n < 1:
        return _emit_error("args", "--min-n must be >= 1", args.json)
    if args.min_n_dist < 1:
        return _emit_error("args", "--min-n-dist must be >= 1", args.json)
    if args.fav_eps < 0:
        return _emit_error("args", "--fav-eps must be >= 0", args.json)
    if args.lookback_days < 1 or args.lookback_days > 90:
        return _emit_error("args", "--lookback-days must be in [1, 90]", args.json)

    now = datetime.now(timezone.utc)

    # ---- DB open (stage: db_open) ----
    try:
        conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return _emit_error("db_open", str(exc), args.json)

    # mode=ro fails lazily on some platforms; force a probe.
    try:
        conn.execute("SELECT 1")
    except sqlite3.Error as exc:
        conn.close()
        return _emit_error("db_open", str(exc), args.json)

    try:
        # ---- Schema precondition (stage: schema, exit 2) ----
        # Fold round 2 (Codex CRITICAL x2): verify the REQUIRED tables + columns
        # exist BEFORE building the report. Without this, a missing/renamed
        # volume_history_cg or a paper_trades column drift would surface as an
        # empty / all-unjoinable report with exit 0 — a silent failure. After
        # this gate, INSUFFICIENT_DATA means STRICTLY "schema OK but
        # n_joinable < min_n," never "schema broken."
        schema_error = _schema_precondition_error(conn)
        if schema_error is not None:
            return _emit_error("schema", schema_error, args.json)

        # ---- Build report (stage: query, exit 2 on sqlite error) ----
        # Fold round 2: any genuine query-time sqlite error now propagates here
        # and maps to exit 2 instead of being swallowed into a silent bucket.
        try:
            report = build_report(
                conn,
                horizons_h=horizons_h,
                min_n=args.min_n,
                min_n_dist=args.min_n_dist,
                fav_eps=args.fav_eps,
                lookback_days=args.lookback_days,
                dedup=not args.no_dedup,
                now=now,
            )
        except sqlite3.Error as exc:
            return _emit_error("query", str(exc), args.json)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(report))
    else:
        print(_format_human(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
