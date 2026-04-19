"""Daily paper trading digest builder for Telegram."""

from __future__ import annotations

import json

import structlog

from scout.db import Database

log = structlog.get_logger()


async def build_paper_digest(db: Database, date_str: str) -> str | None:
    """Build daily paper trading summary for Telegram.

    Queries paper_trades for the given date, computes stats,
    stores into paper_daily_summary, and returns a formatted message.
    Returns None if no trades were opened or closed on the date.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    # Trades opened on the date (M4: exclude long_hold to avoid double-counting)
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM paper_trades WHERE date(opened_at) = ? AND signal_type != 'long_hold'",
        (date_str,),
    )
    row = await cursor.fetchone()
    trades_opened = row[0] or 0

    # Closed trades on the date
    cursor = await conn.execute(
        """SELECT token_id, symbol, pnl_usd, pnl_pct, signal_type
           FROM paper_trades
           WHERE date(closed_at) = ? AND status != 'open'""",
        (date_str,),
    )
    closed_rows = await cursor.fetchall()
    trades_closed = len(closed_rows)

    if trades_opened == 0 and trades_closed == 0:
        return None

    # Win/loss
    wins = sum(1 for r in closed_rows if r[2] is not None and r[2] > 0)
    losses = trades_closed - wins

    # PnL
    total_pnl = sum(float(r[2] or 0) for r in closed_rows)
    win_rate = round((wins / trades_closed) * 100, 1) if trades_closed > 0 else 0.0

    # Best/worst trade
    best_row = max(closed_rows, key=lambda r: float(r[2] or 0)) if closed_rows else None
    worst_row = (
        min(closed_rows, key=lambda r: float(r[2] or 0)) if closed_rows else None
    )

    best_pnl = float(best_row[2] or 0) if best_row else 0
    best_pct = float(best_row[3] or 0) if best_row else 0
    best_symbol = best_row[1] if best_row else "N/A"

    worst_pnl = float(worst_row[2] or 0) if worst_row else 0
    worst_pct = float(worst_row[3] or 0) if worst_row else 0
    worst_symbol = worst_row[1] if worst_row else "N/A"

    # PnL percentages list for avg
    pnl_pcts = [float(r[3] or 0) for r in closed_rows if r[3] is not None]
    avg_pnl_pct = round(sum(pnl_pcts) / len(pnl_pcts), 2) if pnl_pcts else 0.0

    # By signal type
    by_signal: dict[str, dict] = {}
    for r in closed_rows:
        sig = r[4] or "unknown"
        if sig not in by_signal:
            by_signal[sig] = {"trades": 0, "pnl": 0.0, "wins": 0}
        by_signal[sig]["trades"] += 1
        by_signal[sig]["pnl"] += float(r[2] or 0)
        if r[2] is not None and r[2] > 0:
            by_signal[sig]["wins"] += 1

    # Compute win rate per signal type
    by_signal_with_wr: dict[str, dict] = {}
    for sig, data in by_signal.items():
        wr = (
            round((data["wins"] / data["trades"]) * 100, 1) if data["trades"] > 0 else 0
        )
        by_signal_with_wr[sig] = {
            "trades": data["trades"],
            "pnl": round(data["pnl"], 2),
            "win_rate": wr,
        }

    # Open positions + exposure
    cursor = await conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(amount_usd), 0) FROM paper_trades WHERE status = 'open'"
    )
    row = await cursor.fetchone()
    open_count = row[0] or 0
    open_exposure = float(row[1] or 0)

    # Store in paper_daily_summary
    await conn.execute(
        """INSERT OR REPLACE INTO paper_daily_summary
           (date, trades_opened, trades_closed, wins, losses,
            total_pnl_usd, best_trade_pnl, worst_trade_pnl,
            avg_pnl_pct, win_rate_pct, by_signal_type)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            date_str,
            trades_opened,
            trades_closed,
            wins,
            losses,
            round(total_pnl, 2),
            round(best_pnl, 2) if best_row else None,
            round(worst_pnl, 2) if worst_row else None,
            avg_pnl_pct,
            win_rate,
            json.dumps(by_signal_with_wr) if by_signal_with_wr else None,
        ),
    )
    await conn.commit()

    # Format message
    pnl_sign = "+" if total_pnl >= 0 else ""
    lines = [
        f"Paper Trading \u2014 {date_str}",
        "",
        f"Trades: {trades_opened} opened, {trades_closed} closed",
    ]

    if trades_closed > 0:
        lines.append(f"PnL: {pnl_sign}${total_pnl:.2f} (win rate: {win_rate}%)")
        best_sign = "+" if best_pct >= 0 else ""
        best_pnl_sign = "+" if best_pnl >= 0 else ""
        lines.append(
            f"Best: {best_symbol} {best_sign}{best_pct:.1f}% ({best_pnl_sign}${best_pnl:.2f})"
        )
        # L3: If worst trade is still positive, note all trades were profitable
        if worst_pnl >= 0:
            lines.append("Worst: (all trades profitable)")
        else:
            worst_pnl_fmt = f"-${abs(worst_pnl):.2f}"
            worst_sign = "+" if worst_pct >= 0 else ""
            lines.append(
                f"Worst: {worst_symbol} {worst_sign}{worst_pct:.1f}% ({worst_pnl_fmt})"
            )

    if by_signal_with_wr:
        lines.append("")
        lines.append("By signal type:")
        for sig, data in sorted(by_signal_with_wr.items()):
            if data["pnl"] >= 0:
                sig_pnl_fmt = f"+${data['pnl']:.2f}"
            else:
                sig_pnl_fmt = f"-${abs(data['pnl']):.2f}"
            lines.append(
                f"  {sig}: {data['trades']} trades, {sig_pnl_fmt} ({data['win_rate']}% WR)"
            )

    lines.append("")
    lines.append(f"Open: {open_count} positions (${open_exposure:.2f} exposure)")

    log.info(
        "paper_digest_built",
        date=date_str,
        trades_opened=trades_opened,
        trades_closed=trades_closed,
        total_pnl=round(total_pnl, 2),
    )

    return "\n".join(lines)
