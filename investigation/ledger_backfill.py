#!/usr/bin/env python3
"""investigation/ledger_backfill.py — READ-ONLY signal-outcome ledger backfill.

Standalone (stdlib sqlite3 only; imports nothing from scout/). Opens the DB in
read-only mode. For every signal fired in the trailing N days (paper_trades is
the signal-fire record: one row per token x signal_type x opened_at), emits:

  signal_type, token, opened_at, entry_price,
  path: +1h / +6h / +24h / +48h checkpoint pct,
  max_multiple (peak_price/entry), time_to_peak_min (best-effort from
  gainers_snapshots price series where available, else NULL),
  realized: exit_reason / pnl_pct as the configured exit machinery ran it,
  sim_fixed_24h: what a fixed 24h hold would have returned (checkpoint_24h_pct).

Usage (VPS):
  python3 investigation/ledger_backfill.py --db /root/gecko-alpha/scout.db \
      --days 90 --out /tmp/ledger_backfill_90d.csv
"""

import argparse
import csv
import sqlite3
import sys


def ro_connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def time_to_peak_min(conn, coin_id, opened_at, peak_price):
    """Best-effort: first gainers/trending snapshot at/after opened_at whose
    price reaches >=99% of peak_price. NULL when no snapshot series exists."""
    if not peak_price:
        return None
    row = conn.execute(
        """
        SELECT MIN((julianday(snapshot_at) - julianday(?)) * 1440.0) AS m
        FROM gainers_snapshots
        WHERE coin_id = ? AND snapshot_at >= ?
          AND price_at_snapshot >= 0.99 * ?
        """,
        (opened_at, coin_id, opened_at, peak_price),
    ).fetchone()
    return round(row["m"], 1) if row and row["m"] is not None else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out", default="-")
    args = ap.parse_args(argv)

    conn = ro_connect(args.db)
    rows = conn.execute(f"""
        SELECT pt.token_id, pt.symbol, pt.chain, pt.signal_type, pt.opened_at,
               pt.entry_price, pt.status, pt.exit_reason, pt.pnl_pct,
               pt.checkpoint_1h_pct, pt.checkpoint_6h_pct,
               pt.checkpoint_24h_pct, pt.checkpoint_48h_pct,
               pt.peak_price, pt.peak_pct,
               c.quant_score, c.conviction_score, c.signals_fired, c.alerted_at
        FROM paper_trades pt
        LEFT JOIN candidates c ON c.contract_address = pt.token_id
        WHERE pt.opened_at >= datetime('now', '-{args.days} days')
        ORDER BY pt.opened_at
        """).fetchall()

    out = sys.stdout if args.out == "-" else open(args.out, "w", newline="")
    w = csv.writer(out)
    w.writerow(
        [
            "token_id",
            "symbol",
            "chain",
            "signal_type",
            "opened_at",
            "entry_price",
            "quant_score",
            "conviction_score",
            "signals_fired",
            "alerted_at",
            "pct_1h",
            "pct_6h",
            "pct_24h",
            "pct_48h",
            "max_multiple",
            "time_to_peak_min",
            "status",
            "exit_reason",
            "realized_pnl_pct",
            "sim_fixed_24h_pct",
        ]
    )
    for r in rows:
        max_mult = (
            round(r["peak_price"] / r["entry_price"], 3)
            if r["peak_price"] and r["entry_price"]
            else None
        )
        w.writerow(
            [
                r["token_id"],
                r["symbol"],
                r["chain"],
                r["signal_type"],
                r["opened_at"],
                r["entry_price"],
                r["quant_score"],
                r["conviction_score"],
                r["signals_fired"],
                r["alerted_at"],
                r["checkpoint_1h_pct"],
                r["checkpoint_6h_pct"],
                r["checkpoint_24h_pct"],
                r["checkpoint_48h_pct"],
                max_mult,
                time_to_peak_min(conn, r["token_id"], r["opened_at"], r["peak_price"]),
                r["status"],
                r["exit_reason"],
                r["pnl_pct"],
                r["checkpoint_24h_pct"],
            ]
        )

    # Aggregate footer to stderr so the CSV stays clean.
    agg = conn.execute(f"""
        SELECT COUNT(*) n,
               ROUND(AVG(checkpoint_24h_pct), 2) fixed24,
               ROUND(AVG(pnl_pct), 2) realized,
               ROUND(AVG(peak_pct), 2) peak
        FROM paper_trades
        WHERE opened_at >= datetime('now', '-{args.days} days')
          AND status != 'open'
        """).fetchone()
    print(
        f"[ledger_backfill] closed n={agg['n']} avg_fixed24h={agg['fixed24']}% "
        f"avg_realized={agg['realized']}% avg_peak={agg['peak']}%",
        file=sys.stderr,
    )
    if out is not sys.stdout:
        out.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
