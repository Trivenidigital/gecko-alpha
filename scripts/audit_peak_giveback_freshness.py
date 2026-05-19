"""Read-only historical audit for stale-entry peak giveback.

This script compares the first observed production snapshot for a token to the
best pre-entry snapshot and the eventual paper-trade entry. It is intentionally
analysis-only: no writes, no gate changes, no trading behavior changes.

Usage:
    python scripts/audit_peak_giveback_freshness.py --db scout.db --since "2026-05-01 14:06:00"
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sqlite3
from dataclasses import dataclass
from statistics import median
from typing import Any


PRICE_SOURCES = {
    "gainers_snapshots": ("snapshot_at", "price_at_snapshot"),
    "losers_snapshots": ("snapshot_at", "price_at_snapshot"),
    "volume_history_cg": ("recorded_at", "price"),
    "volume_spikes": ("detected_at", "price"),
    "momentum_7d": ("detected_at", "current_price"),
}


@dataclass(frozen=True)
class AuditRow:
    trade_id: int
    token_id: str
    symbol: str
    signal_type: str
    opened_at: dt.datetime
    entry_price: float
    pnl_usd: float
    pnl_pct: float
    peak_pct: float | None
    first_seen_at: dt.datetime
    first_price: float
    pre_entry_peak_at: dt.datetime
    pre_entry_peak_price: float

    @property
    def freshness_minutes(self) -> float:
        return (self.opened_at - self.first_seen_at).total_seconds() / 60.0

    @property
    def entry_gain_from_first_pct(self) -> float:
        return pct_change(self.first_price, self.entry_price)

    @property
    def pre_entry_peak_gain_pct(self) -> float:
        return pct_change(self.first_price, self.pre_entry_peak_price)

    @property
    def pre_entry_giveback_pp(self) -> float:
        return max(0.0, self.pre_entry_peak_gain_pct - self.entry_gain_from_first_pct)

    @property
    def pre_entry_giveback_ratio(self) -> float:
        peak_gain = self.pre_entry_peak_gain_pct
        if peak_gain <= 0:
            return 0.0
        return max(0.0, self.pre_entry_giveback_pp / peak_gain)


def parse_time(value: Any) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def num(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def pct_change(start: float, end: float) -> float:
    if start <= 0:
        return 0.0
    return (end - start) / start * 100.0


def table_columns(cur: sqlite3.Cursor, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in cur.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def load_price_path(
    cur: sqlite3.Cursor, token_id: str, opened_at: dt.datetime
) -> list[tuple[dt.datetime, float, str]]:
    out: list[tuple[dt.datetime, float, str]] = []
    opened_text = opened_at.isoformat()
    for table, (time_col, price_col) in PRICE_SOURCES.items():
        if not table_columns(cur, table):
            continue
        rows = cur.execute(
            f"""SELECT {time_col} AS ts, {price_col} AS price
                FROM {table}
                WHERE coin_id = ?
                  AND {price_col} IS NOT NULL
                  AND datetime({time_col}) <= datetime(?)
                ORDER BY datetime({time_col}) ASC""",
            (token_id, opened_text),
        ).fetchall()
        for row in rows:
            ts = parse_time(row["ts"])
            price = num(row["price"])
            if ts is not None and price is not None and price > 0:
                out.append((ts, price, table))
    out.sort(key=lambda item: item[0])
    return out


def build_rows(cur: sqlite3.Cursor, since: str | None) -> tuple[list[AuditRow], dict[str, int]]:
    where = "status LIKE 'closed_%' AND pnl_usd IS NOT NULL AND entry_price > 0"
    params: list[Any] = []
    if since:
        where += " AND datetime(opened_at) >= datetime(?)"
        params.append(since)
    trades = cur.execute(
        f"""SELECT id, token_id, symbol, signal_type, opened_at, entry_price,
                   pnl_usd, pnl_pct, peak_pct
            FROM paper_trades
            WHERE {where}
            ORDER BY datetime(opened_at) ASC""",
        params,
    ).fetchall()

    rows: list[AuditRow] = []
    skips = {
        "closed_trades": len(trades),
        "missing_opened_at": 0,
        "missing_pre_entry_path": 0,
        "bad_trade_value": 0,
    }
    for trade in trades:
        opened_at = parse_time(trade["opened_at"])
        entry_price = num(trade["entry_price"])
        pnl_usd = num(trade["pnl_usd"])
        pnl_pct = num(trade["pnl_pct"])
        if opened_at is None:
            skips["missing_opened_at"] += 1
            continue
        if entry_price is None or pnl_usd is None or pnl_pct is None:
            skips["bad_trade_value"] += 1
            continue
        path = load_price_path(cur, str(trade["token_id"]), opened_at)
        if not path:
            skips["missing_pre_entry_path"] += 1
            continue
        first_at, first_price, _first_source = path[0]
        peak_at, peak_price, _peak_source = max(path, key=lambda item: item[1])
        rows.append(
            AuditRow(
                trade_id=int(trade["id"]),
                token_id=str(trade["token_id"]),
                symbol=str(trade["symbol"]),
                signal_type=str(trade["signal_type"]),
                opened_at=opened_at,
                entry_price=entry_price,
                pnl_usd=pnl_usd,
                pnl_pct=pnl_pct,
                peak_pct=num(trade["peak_pct"]),
                first_seen_at=first_at,
                first_price=first_price,
                pre_entry_peak_at=peak_at,
                pre_entry_peak_price=peak_price,
            )
        )
    return rows, skips


def stats(rows: list[AuditRow]) -> dict[str, Any]:
    n = len(rows)
    net = sum(row.pnl_usd for row in rows)
    wins = sum(1 for row in rows if row.pnl_usd > 0)
    return {
        "n": n,
        "net": net,
        "avg": net / n if n else 0.0,
        "win_pct": wins / n * 100.0 if n else 0.0,
        "median_freshness_h": median([row.freshness_minutes / 60.0 for row in rows]) if rows else 0.0,
        "median_pre_entry_giveback_pp": median([row.pre_entry_giveback_pp for row in rows]) if rows else 0.0,
        "median_pre_entry_giveback_ratio": median([row.pre_entry_giveback_ratio for row in rows]) if rows else 0.0,
    }


def fmt_money(value: float) -> str:
    return f"${value:,.2f}"


def print_filter_sweep(title: str, rows: list[AuditRow], filters: list[tuple[str, list[AuditRow]]]) -> None:
    print(f"\n## {title}\n")
    print("| Rule | Rejected n | Rejected net | Rejected avg | Rejected win | Kept n | Kept net | Avoided P&L if rejected |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    total_ids = {row.trade_id for row in rows}
    for label, rejected in filters:
        rejected_ids = {row.trade_id for row in rejected}
        kept = [row for row in rows if row.trade_id in total_ids - rejected_ids]
        rej = stats(rejected)
        keep = stats(kept)
        avoided = -rej["net"]
        print(
            f"| {label} | {rej['n']} | {fmt_money(rej['net'])} | {fmt_money(rej['avg'])} | "
            f"{rej['win_pct']:.1f}% | {keep['n']} | {fmt_money(keep['net'])} | {fmt_money(avoided)} |"
        )


def bucket_by_signal(rows: list[AuditRow]) -> None:
    print("\n## Worst stale-entry examples\n")
    print("| Trade | Symbol | Signal | Fresh h | Pre-entry peak gain | Entry gain | Giveback pp | Giveback ratio | P&L |")
    print("|---:|---|---|---:|---:|---:|---:|---:|---:|")
    ranked = sorted(
        rows,
        key=lambda row: (row.pre_entry_giveback_pp, row.pre_entry_peak_gain_pct),
        reverse=True,
    )
    for row in ranked[:20]:
        print(
            f"| {row.trade_id} | {row.symbol} | {row.signal_type} | "
            f"{row.freshness_minutes / 60.0:.1f} | {row.pre_entry_peak_gain_pct:.1f}% | "
            f"{row.entry_gain_from_first_pct:.1f}% | {row.pre_entry_giveback_pp:.1f} | "
            f"{row.pre_entry_giveback_ratio:.2f} | {fmt_money(row.pnl_usd)} |"
        )

    grouped: dict[str, list[AuditRow]] = {}
    for row in rows:
        grouped.setdefault(row.signal_type, []).append(row)
    print("\n## Signal-level stale-entry coverage\n")
    print("| Signal | n | Net | Avg | Win | Median fresh h | Median pre-entry giveback pp | Median giveback ratio |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    for signal, items in sorted(grouped.items(), key=lambda kv: stats(kv[1])["net"]):
        stat = stats(items)
        print(
            f"| {signal} | {stat['n']} | {fmt_money(stat['net'])} | {fmt_money(stat['avg'])} | "
            f"{stat['win_pct']:.1f}% | {stat['median_freshness_h']:.1f} | "
            f"{stat['median_pre_entry_giveback_pp']:.1f} | {stat['median_pre_entry_giveback_ratio']:.2f} |"
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="scout.db")
    parser.add_argument("--since")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    rows, skips = build_rows(cur, args.since)
    overall = stats(rows)

    print("# Peak-giveback / freshness historical audit")
    print()
    print(f"- DB: `{args.db}`")
    print(f"- Since: `{args.since or 'all closed trades'}`")
    print(f"- Coverage: `{json.dumps(skips, sort_keys=True)}`")
    print(
        f"- Analyzed stale-entry cohort: {overall['n']} trades, net {fmt_money(overall['net'])}, "
        f"avg {fmt_money(overall['avg'])}, win {overall['win_pct']:.1f}%"
    )
    print(
        f"- Median freshness: {overall['median_freshness_h']:.1f}h; "
        f"median pre-entry giveback: {overall['median_pre_entry_giveback_pp']:.1f}pp "
        f"({overall['median_pre_entry_giveback_ratio']:.2f} of pre-entry peak gain)"
    )

    ttl_filters = [
        (f"freshness > {hours}h", [row for row in rows if row.freshness_minutes > hours * 60.0])
        for hours in (0.5, 1, 2, 6, 12, 24, 48, 72)
    ]
    print_filter_sweep("TTL-only sweep", rows, ttl_filters)

    giveback_filters: list[tuple[str, list[AuditRow]]] = []
    for min_peak in (10, 20, 40, 75):
        for ratio in (0.30, 0.50, 0.60, 0.70):
            rejected = [
                row
                for row in rows
                if row.pre_entry_peak_gain_pct >= min_peak
                and row.pre_entry_giveback_ratio >= ratio
            ]
            giveback_filters.append((f"pre-entry peak >= {min_peak}% and giveback >= {ratio:.0%}", rejected))
    print_filter_sweep("Pre-entry giveback-ratio sweep", rows, giveback_filters)

    pp_filters = [
        (
            f"pre-entry giveback >= {pp}pp",
            [row for row in rows if row.pre_entry_giveback_pp >= pp],
        )
        for pp in (10, 15, 20, 30, 45, 60)
    ]
    print_filter_sweep("Pre-entry giveback-pp sweep", rows, pp_filters)

    combined_filters: list[tuple[str, list[AuditRow]]] = []
    for hours in (6, 12, 24, 48):
        for ratio in (0.50, 0.60, 0.70):
            rejected = [
                row
                for row in rows
                if row.freshness_minutes > hours * 60.0
                and row.pre_entry_peak_gain_pct >= 20
                and row.pre_entry_giveback_ratio >= ratio
            ]
            combined_filters.append((f"fresh>{hours}h + peak>=20% + giveback>={ratio:.0%}", rejected))
    print_filter_sweep("Combined TTL + giveback sweep", rows, combined_filters)

    bucket_by_signal(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
