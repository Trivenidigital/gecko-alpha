"""EVALUATE phase -- paper trade checkpoint tracking with TP/SL/expiry.

Runs every 30 minutes. Uses batch price lookup from price_cache
(single SELECT ... WHERE coin_id IN (...) query).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog

from scout.db import Database
from scout.trading.paper import PaperTrader

log = structlog.get_logger()

_trader = PaperTrader()


async def evaluate_paper_trades(db: Database, settings) -> None:
    """Check all open paper trades: update checkpoints, check TP/SL, expire old.

    Uses a single batch query to fetch prices for all open trades.
    Logs price_age_seconds alongside the price for each trade.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    # 1. Get all open trades
    cursor = await conn.execute(
        """SELECT id, token_id, entry_price, opened_at,
                  tp_price, sl_price, tp_pct, sl_pct,
                  checkpoint_1h_price, checkpoint_6h_price,
                  checkpoint_24h_price, checkpoint_48h_price,
                  peak_price, peak_pct, amount_usd, quantity
           FROM paper_trades
           WHERE status = 'open'"""
    )
    rows = await cursor.fetchall()
    if not rows:
        return

    # 2. Batch-fetch current prices from price_cache (single IN query)
    unique_ids = list({row[1] for row in rows})
    placeholders = ",".join("?" * len(unique_ids))
    price_cursor = await conn.execute(
        f"""SELECT coin_id, current_price, updated_at
            FROM price_cache
            WHERE coin_id IN ({placeholders})""",
        unique_ids,
    )
    price_rows = await price_cursor.fetchall()
    price_map: dict[str, tuple[float, str]] = {}
    for pr in price_rows:
        if pr[1] is not None:
            price_map[pr[0]] = (float(pr[1]), str(pr[2]))

    now = datetime.now(timezone.utc)
    max_duration = timedelta(hours=settings.PAPER_MAX_DURATION_HOURS)
    slippage_bps = settings.PAPER_SLIPPAGE_BPS

    for row in rows:
        trade_id = row[0]
        token_id = row[1]
        entry_price = float(row[2])
        opened_at = datetime.fromisoformat(str(row[3])).replace(tzinfo=timezone.utc)
        tp_price = float(row[4])
        sl_price = float(row[5])
        cp_1h = row[8]
        cp_6h = row[9]
        cp_24h = row[10]
        cp_48h = row[11]
        peak_price = float(row[12]) if row[12] is not None else None
        peak_pct = float(row[13]) if row[13] is not None else None

        # Price lookup
        price_data = price_map.get(token_id)
        if price_data is None:
            log.debug("trade_eval_no_price", trade_id=trade_id, token_id=token_id)
            continue

        current_price, updated_at_str = price_data
        updated_at = datetime.fromisoformat(updated_at_str).replace(tzinfo=timezone.utc)
        price_age_seconds = (now - updated_at).total_seconds()

        if entry_price <= 0:
            continue

        elapsed = now - opened_at
        change_pct = ((current_price - entry_price) / entry_price) * 100

        # --- Peak tracking ---
        reference = peak_price if peak_price is not None else entry_price
        if current_price > reference:
            peak_price = current_price
            peak_pct = ((current_price - entry_price) / entry_price) * 100
            await conn.execute(
                "UPDATE paper_trades SET peak_price = ?, peak_pct = ? WHERE id = ?",
                (peak_price, round(peak_pct, 4), trade_id),
            )

        # --- Checkpoint updates ---
        updates: dict[str, object] = {}

        if cp_1h is None and elapsed >= timedelta(hours=1):
            updates["checkpoint_1h_price"] = current_price
            updates["checkpoint_1h_pct"] = round(change_pct, 4)

        if cp_6h is None and elapsed >= timedelta(hours=6):
            updates["checkpoint_6h_price"] = current_price
            updates["checkpoint_6h_pct"] = round(change_pct, 4)

        if cp_24h is None and elapsed >= timedelta(hours=24):
            updates["checkpoint_24h_price"] = current_price
            updates["checkpoint_24h_pct"] = round(change_pct, 4)

        if cp_48h is None and elapsed >= timedelta(hours=48):
            updates["checkpoint_48h_price"] = current_price
            updates["checkpoint_48h_pct"] = round(change_pct, 4)

        if updates:
            set_clause = ", ".join(f"{k} = ?" for k in updates)
            values = list(updates.values()) + [trade_id]
            await conn.execute(
                f"UPDATE paper_trades SET {set_clause} WHERE id = ?",
                values,
            )

        # --- TP/SL/Expiry checks (takes priority, but checkpoints still recorded above) ---
        close_reason = None
        if current_price >= tp_price:
            close_reason = "take_profit"
        elif current_price <= sl_price:
            close_reason = "stop_loss"
        elif elapsed >= max_duration:
            close_reason = "expired"

        if close_reason is not None:
            await _trader.execute_sell(
                db=db,
                trade_id=trade_id,
                current_price=current_price,
                reason=close_reason,
                slippage_bps=slippage_bps,
            )
            log.info(
                "paper_trade_eval_closed",
                trade_id=trade_id,
                token_id=token_id,
                reason=close_reason,
                price_age_seconds=round(price_age_seconds, 1),
                current_price=current_price,
                change_pct=round(change_pct, 2),
            )
        else:
            log.debug(
                "paper_trade_eval_ok",
                trade_id=trade_id,
                token_id=token_id,
                price_age_seconds=round(price_age_seconds, 1),
                current_price=current_price,
                change_pct=round(change_pct, 2),
            )

    await conn.commit()
