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
                  peak_price, peak_pct, signal_data, symbol, name, chain,
                  amount_usd, quantity, signal_type
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

    # M5: Instantiate inside function instead of module-level singleton
    _trader = PaperTrader()

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

        if price_age_seconds > 3600:  # 1 hour max for evaluator
            log.debug("trade_eval_stale_price", trade_id=trade_id, age=round(price_age_seconds, 1))
            continue

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
        elif sl_price > 0 and current_price <= sl_price:
            close_reason = "stop_loss"
        elif elapsed >= max_duration:
            close_reason = "expired"

        if close_reason is not None:
            # M3: Log how long ago expiry was actually due (useful after offline gaps)
            if close_reason == "expired":
                delay_seconds = (elapsed - max_duration).total_seconds()
                if delay_seconds > 0:
                    log.info(
                        "trade_expired_delayed",
                        trade_id=trade_id,
                        token_id=token_id,
                        delay_hours=round(delay_seconds / 3600, 1),
                    )

            row_signal_type = row[20] if len(row) > 20 else ""
            if close_reason == "take_profit" and row_signal_type != "long_hold":
                # Partial TP: sell 70%, keep 30% as long-term hold
                tp_sell_pct = getattr(settings, "PAPER_TP_SELL_PCT", 70.0) / 100.0
                original_amount = float(row[18])  # amount_usd
                original_qty = float(row[19])      # quantity
                sell_amount = original_amount * tp_sell_pct
                keep_amount = original_amount * (1 - tp_sell_pct)
                keep_qty = original_qty * (1 - tp_sell_pct)

                # Close the original trade (records PnL on the 70% sold)
                # First update amount to reflect only the sold portion
                await conn.execute(
                    "UPDATE paper_trades SET amount_usd = ?, quantity = ? WHERE id = ? AND status = 'open'",
                    (sell_amount, original_qty * tp_sell_pct, trade_id),
                )
                sold = await _trader.execute_sell(
                    db=db, trade_id=trade_id,
                    current_price=current_price, reason=close_reason,
                    slippage_bps=slippage_bps,
                )
                if not sold:
                    log.warning("partial_tp_sell_failed", trade_id=trade_id)
                    continue  # don't create long_hold if sell failed

                # Open a new "long_hold" trade for the remaining 30%
                if keep_amount > 0:
                    signal_data_raw = row[14] if len(row) > 14 else "{}"
                    new_id = await _trader.execute_buy(
                        db=db, token_id=token_id,
                        symbol=row[15] if len(row) > 15 else "",
                        name=row[16] if len(row) > 16 else "",
                        chain=row[17] if len(row) > 17 else "coingecko",
                        signal_type="long_hold",
                        signal_data={"origin_trade_id": trade_id, "origin_signal": str(signal_data_raw)},
                        current_price=current_price,
                        amount_usd=keep_amount,
                        tp_pct=100.0,  # very high TP for long hold
                        sl_pct=0.0,    # no stop loss
                        slippage_bps=0, # no additional slippage on the hold portion
                    )
                    if new_id is None:
                        log.warning("long_hold_creation_failed", trade_id=trade_id, token_id=token_id)
                    else:
                        log.info(
                            "paper_trade_partial_tp",
                            trade_id=trade_id, token_id=token_id,
                            sold_pct=tp_sell_pct * 100,
                            keep_amount=round(keep_amount, 2),
                        )
            else:
                # SL, expiry, or long_hold TP: close the full position
                closed = await _trader.execute_sell(
                    db=db, trade_id=trade_id,
                    current_price=current_price, reason=close_reason,
                    slippage_bps=slippage_bps,
                )

            # For partial TP path, execute_sell was called inline above
            if close_reason == "take_profit" and row_signal_type != "long_hold":
                closed = True
            if closed:
                log.info(
                    "paper_trade_eval_closed",
                    trade_id=trade_id, token_id=token_id,
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
