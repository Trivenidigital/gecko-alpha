#!/usr/bin/env python3
"""investigation/time_death_counterfactual.py — READ-ONLY time_death adjudication.

Per the 2026-07-20 review ruling: for each REAL time_death close, report

    actual_time_death_pnl
    counterfactual_normal_exit_pnl
    incremental_benefit = actual_time_death_pnl - counterfactual_normal_exit_pnl

where the counterfactual replays the strategy's ACTUAL alternative exit
rules forward over the post-close historical price path — never future
maxima, never any look-ahead: the path is walked chronologically and the
first triggering rule wins.

Evidence-class separation (never blended into one headline number):
  measured    — full replay resolved on covered price data (a counterfactual
                exit fired, or the expiry boundary fell inside coverage)
  unresolved  — price coverage ended before any exit rule fired; EXCLUDED
                from the primary measured result, counted in coverage stats
  dry_run_era — closes at/before --dry-run-cutoff-ts (PAPER_TIME_DEATH_DRY_RUN
                observation era); reported separately, never merged with live

Replayed rule set (parameter defaults = repo Settings; override via CLI):
  stop_loss      price <= entry * (1 - sl_pct/100)
  floor          if floor_armed: price <= entry after arming
  trailing_stop  armed at peak >= activation_pct; fires at
                 peak * (1 - drawdown_pct/100), floored at entry*(1+floor_pct/100)
  peak_fade      armed at peak >= fade_min_peak_pct; fires when price
                 retraces to peak_pct * retrace_ratio
  expired        at opened_at + max_duration_h the trade closes at the last
                 covered price at/before that boundary
NOT modeled (documented limitation, per-trade flagged where detectable):
moonshot ladders / partial sells, high_peak_fade, stale forced-closes.
Trades with remaining_qty partial state are marked unresolved.

Price path: gainers_snapshots (coin_id, snapshot_at, price_at_snapshot)
strictly AFTER the time_death close, seeded with the close price itself.
7-day retention means older closes will be unresolved — that is expected
and is exactly why coverage percentage is a first-class output.

Usage (VPS):
  python3 investigation/time_death_counterfactual.py \
      --db /root/gecko-alpha/scout.db \
      --dry-run-cutoff-ts 2026-07-17T12:28:52.954712Z \
      --out /tmp/time_death_counterfactual.csv
"""

import argparse
import csv
import sqlite3
import sys


def ro(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def replay_counterfactual(
    entry_price: float,
    close_price: float,
    path: list,  # [(ts, price), ...] strictly after the time_death close
    expiry_boundary_ts: str,
    *,
    sl_pct: float,
    trail_activation_pct: float,
    trail_drawdown_pct: float,
    trail_floor_pct: float,
    fade_min_peak_pct: float,
    fade_retrace_ratio: float,
    floor_armed: bool,
) -> dict:
    """Walk the path forward; first triggering rule wins. No look-ahead.

    Returns {resolved, exit_reason, exit_price, exit_ts}; resolved=False when
    coverage ends before any rule fires and before the expiry boundary.
    """
    peak = max(entry_price, close_price)
    sl_price = entry_price * (1.0 - sl_pct / 100.0)
    last_ts, last_price = None, close_price

    for ts, price in path:
        if price is None or price <= 0:
            continue
        if ts > expiry_boundary_ts:
            # Expiry fell inside coverage: close at last covered price.
            return {
                "resolved": True,
                "exit_reason": "expired",
                "exit_price": last_price,
                "exit_ts": last_ts or ts,
            }
        peak = max(peak, price)
        peak_pct = (peak / entry_price - 1.0) * 100.0
        if not floor_armed and price <= sl_price:
            return {
                "resolved": True,
                "exit_reason": "stop_loss",
                "exit_price": price,
                "exit_ts": ts,
            }
        if floor_armed and price <= entry_price:
            return {
                "resolved": True,
                "exit_reason": "floor",
                "exit_price": price,
                "exit_ts": ts,
            }
        if peak_pct >= trail_activation_pct:
            trigger = max(
                peak * (1.0 - trail_drawdown_pct / 100.0),
                entry_price * (1.0 + trail_floor_pct / 100.0),
            )
            if price <= trigger:
                return {
                    "resolved": True,
                    "exit_reason": "trailing_stop",
                    "exit_price": price,
                    "exit_ts": ts,
                }
        elif peak_pct >= fade_min_peak_pct:
            fade_trigger = entry_price * (1.0 + peak_pct * fade_retrace_ratio / 100.0)
            if price <= fade_trigger:
                return {
                    "resolved": True,
                    "exit_reason": "peak_fade",
                    "exit_price": price,
                    "exit_ts": ts,
                }
        last_ts, last_price = ts, price

    return {"resolved": False, "exit_reason": None, "exit_price": None, "exit_ts": None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--signal", default="gainers_early")
    ap.add_argument(
        "--dry-run-cutoff-ts",
        default=None,
        help="closes at/before this ISO ts are classed dry_run_era",
    )
    ap.add_argument("--out", default="-")
    ap.add_argument("--sl-pct", type=float, default=15.0)
    ap.add_argument("--trail-activation-pct", type=float, default=10.0)
    ap.add_argument("--trail-drawdown-pct", type=float, default=10.0)
    ap.add_argument("--trail-floor-pct", type=float, default=3.0)
    ap.add_argument("--fade-min-peak-pct", type=float, default=10.0)
    ap.add_argument("--fade-retrace-ratio", type=float, default=0.7)
    ap.add_argument("--max-duration-h", type=int, default=48)
    args = ap.parse_args(argv)

    conn = ro(args.db)
    trades = conn.execute(
        """
        SELECT rowid AS trade_rowid, token_id, symbol, opened_at, closed_at,
               entry_price, exit_price, pnl_usd, pnl_pct, position_size_usd,
               COALESCE(floor_armed, 0) AS floor_armed,
               remaining_qty
        FROM paper_trades
        WHERE signal_type = ? AND status != 'open' AND exit_reason = 'time_death'
        ORDER BY closed_at
        """,
        (args.signal,),
    ).fetchall()

    out = sys.stdout if args.out == "-" else open(args.out, "w", newline="")
    w = csv.writer(out)
    w.writerow(
        [
            "trade_rowid",
            "token_id",
            "symbol",
            "opened_at",
            "closed_at",
            "era",
            "coverage_class",
            "price_points",
            "actual_time_death_pnl",
            "counterfactual_exit_reason",
            "counterfactual_normal_exit_pnl",
            "incremental_benefit",
        ]
    )

    measured_actual = measured_cf = 0.0
    n_measured = n_unresolved = n_dry = 0
    for t in trades:
        era = (
            "dry_run_era"
            if args.dry_run_cutoff_ts and t["closed_at"] <= args.dry_run_cutoff_ts
            else "live"
        )
        # Expiry boundary from opened_at (SQLite datetime string arithmetic).
        boundary = conn.execute(
            "SELECT datetime(?, ?)",
            (t["opened_at"], f"+{args.max_duration_h} hours"),
        ).fetchone()[0]
        path = conn.execute(
            """
            SELECT snapshot_at, price_at_snapshot FROM gainers_snapshots
            WHERE coin_id = ? AND snapshot_at > ?
            ORDER BY snapshot_at
            """,
            (t["token_id"], t["closed_at"]),
        ).fetchall()

        partial = t["remaining_qty"] is not None and t["remaining_qty"] not in (0, 1)
        entry = t["entry_price"]
        size = t["position_size_usd"] or 0.0
        result = (
            {"resolved": False}
            if (partial or not entry)
            else replay_counterfactual(
                entry,
                t["exit_price"] or entry,
                [(r["snapshot_at"], r["price_at_snapshot"]) for r in path],
                boundary,
                sl_pct=args.sl_pct,
                trail_activation_pct=args.trail_activation_pct,
                trail_drawdown_pct=args.trail_drawdown_pct,
                trail_floor_pct=args.trail_floor_pct,
                fade_min_peak_pct=args.fade_min_peak_pct,
                fade_retrace_ratio=args.fade_retrace_ratio,
                floor_armed=bool(t["floor_armed"]),
            )
        )

        if result["resolved"]:
            cf_pnl = round(size * (result["exit_price"] / entry - 1.0), 2)
            incremental = round((t["pnl_usd"] or 0.0) - cf_pnl, 2)
            coverage_class = "measured"
            if era == "live":
                n_measured += 1
                measured_actual += t["pnl_usd"] or 0.0
                measured_cf += cf_pnl
            else:
                n_dry += 1
            w.writerow(
                [
                    t["trade_rowid"],
                    t["token_id"],
                    t["symbol"],
                    t["opened_at"],
                    t["closed_at"],
                    era,
                    coverage_class,
                    len(path),
                    t["pnl_usd"],
                    result["exit_reason"],
                    cf_pnl,
                    incremental,
                ]
            )
        else:
            n_unresolved += 1
            w.writerow(
                [
                    t["trade_rowid"],
                    t["token_id"],
                    t["symbol"],
                    t["opened_at"],
                    t["closed_at"],
                    era,
                    "unresolved_partial" if partial else "unresolved_coverage",
                    len(path),
                    t["pnl_usd"],
                    None,
                    None,
                    None,
                ]
            )

    total = len(trades)
    coverage_pct = round(100.0 * (total - n_unresolved) / total, 1) if total else None
    print(
        f"[time_death_counterfactual] trades={total} "
        f"measured_live={n_measured} dry_run_era_measured={n_dry} "
        f"unresolved={n_unresolved} coverage_pct={coverage_pct} | "
        f"MEASURED-LIVE ONLY: actual={round(measured_actual, 2)} "
        f"counterfactual={round(measured_cf, 2)} "
        f"incremental_benefit={round(measured_actual - measured_cf, 2)}",
        file=sys.stderr,
    )
    if out is not sys.stdout:
        out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
