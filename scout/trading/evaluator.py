"""EVALUATE phase -- paper trade checkpoint tracking with TP/SL/expiry.

Runs every 30 minutes. Uses batch price lookup from price_cache
(single SELECT ... WHERE coin_id IN (...) query).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import structlog

from scout.db import Database
from scout.trading.paper import PaperTrader

log = structlog.get_logger()


async def _load_bl061_cutover_ts(conn) -> str | None:
    """Load BL-061 cutover timestamp from paper_migrations.

    Returns None if the row is missing (fresh DB before initialize() ran,
    shouldn't happen in practice). Callers should treat None as "no cutover
    — all rows use new ladder policy."
    """
    cur = await conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name = 'bl061_ladder'"
    )
    row = await cur.fetchone()
    return row[0] if row else None


async def evaluate_paper_trades(db: Database, settings) -> None:
    """Check all open paper trades: update checkpoints, check TP/SL, expire old.

    Uses a single batch query to fetch prices for all open trades.
    Logs price_age_seconds alongside the price for each trade.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    # 1. Get all open trades
    cursor = await conn.execute("""SELECT id, token_id, entry_price, opened_at,
                  tp_price, sl_price, tp_pct, sl_pct,
                  checkpoint_1h_price, checkpoint_6h_price,
                  checkpoint_24h_price, checkpoint_48h_price,
                  peak_price, peak_pct, signal_data, symbol, name, chain,
                  amount_usd, quantity, signal_type,
                  created_at, leg_1_filled_at, leg_2_filled_at,
                  remaining_qty, floor_armed, realized_pnl_usd
           FROM paper_trades
           WHERE status = 'open'""")
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

    _trader = PaperTrader()
    cutover_ts = await _load_bl061_cutover_ts(conn)

    now = datetime.now(timezone.utc)
    max_duration = timedelta(hours=settings.PAPER_MAX_DURATION_HOURS)
    slippage_bps = settings.PAPER_SLIPPAGE_BPS

    for row in rows:
        trade_id = row[0]
        try:
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

            price_data = price_map.get(token_id)
            if price_data is None:
                log.info("trade_eval_no_price", trade_id=trade_id, token_id=token_id)
                continue

            current_price, updated_at_str = price_data
            updated_at = datetime.fromisoformat(updated_at_str).replace(
                tzinfo=timezone.utc
            )
            price_age_seconds = (now - updated_at).total_seconds()

            if price_age_seconds > 3600:  # 1 hour max for evaluator
                log.info(
                    "trade_eval_stale_price",
                    trade_id=trade_id,
                    token_id=token_id,
                    age=round(price_age_seconds, 1),
                )
                continue

            if not math.isfinite(entry_price) or entry_price <= 0:
                log.warning(
                    "trade_eval_bad_entry_price",
                    trade_id=trade_id,
                    token_id=token_id,
                    entry_price=entry_price,
                )
                continue

            elapsed = now - opened_at
            change_pct = ((current_price - entry_price) / entry_price) * 100

            reference = peak_price if peak_price is not None else entry_price
            if current_price > reference:
                peak_price = current_price
                peak_pct = ((current_price - entry_price) / entry_price) * 100
                await conn.execute(
                    "UPDATE paper_trades SET peak_price = ?, peak_pct = ? WHERE id = ?",
                    (peak_price, round(peak_pct, 4), trade_id),
                )

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

            # BL-061 ladder state
            created_at_str = row[21] if len(row) > 21 else None
            leg_1_filled = row[22] if len(row) > 22 else None
            leg_2_filled = row[23] if len(row) > 23 else None
            remaining_qty = float(row[24]) if len(row) > 24 and row[24] is not None else None
            floor_armed = bool(row[25]) if len(row) > 25 and row[25] is not None else False

            # Determine BL-061 eligibility: use datetime comparison to handle
            # format mismatch between SQLite datetime('now') space format and
            # ISO-with-tz format stored in paper_migrations.cutover_ts.
            # SQLite datetime('now') has second precision; cutover_ts has
            # microsecond precision. Truncate cutover to the second so that
            # trades inserted in the same second as the migration are included.
            def _parse_ts(s: str | None) -> datetime | None:
                if s is None:
                    return None
                try:
                    dt = datetime.fromisoformat(s)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt
                except ValueError:
                    return None

            created_at_dt = _parse_ts(created_at_str)
            cutover_dt = _parse_ts(cutover_ts)
            if cutover_dt is not None:
                cutover_dt = cutover_dt.replace(microsecond=0)

            is_bl061 = (
                remaining_qty is not None
                and created_at_dt is not None
                and cutover_dt is not None
                and created_at_dt >= cutover_dt
            )

            if is_bl061:
                close_reason = None
                close_status: str | None = None
                # SL applies only before leg 1 arms the floor
                if not floor_armed and sl_price > 0 and current_price <= sl_price:
                    close_reason = "stop_loss"
                    close_status = "closed_sl"
                # Leg 1
                elif leg_1_filled is None and change_pct >= settings.PAPER_LADDER_LEG_1_PCT:
                    await _trader.execute_partial_sell(
                        db=db, trade_id=trade_id, leg=1,
                        sell_qty_frac=settings.PAPER_LADDER_LEG_1_QTY_FRAC,
                        current_price=current_price, slippage_bps=slippage_bps,
                    )
                    continue
                # Leg 2
                elif (
                    leg_1_filled is not None
                    and leg_2_filled is None
                    and change_pct >= settings.PAPER_LADDER_LEG_2_PCT
                ):
                    await _trader.execute_partial_sell(
                        db=db, trade_id=trade_id, leg=2,
                        sell_qty_frac=settings.PAPER_LADDER_LEG_2_QTY_FRAC,
                        current_price=current_price, slippage_bps=slippage_bps,
                    )
                    continue
                # Floor exit — once armed, don't let the runner slice close below entry
                elif floor_armed and current_price <= entry_price:
                    close_reason = "floor"
                    close_status = "closed_floor"
                    log.info(
                        "floor_exit",
                        trade_id=trade_id, peak_pct=round(peak_pct or 0, 2),
                        current_price=current_price,
                    )
                # Trailing stop on runner (post-leg-1 only)
                elif (
                    floor_armed
                    and peak_price is not None
                    and peak_pct is not None
                    and peak_pct >= settings.PAPER_LADDER_LEG_1_PCT
                ):
                    trail_threshold = peak_price * (
                        1 - settings.PAPER_LADDER_TRAIL_PCT / 100.0
                    )
                    if current_price < trail_threshold:
                        close_reason = "trailing_stop"
                        close_status = "closed_trailing_stop"
                # Expiry
                elif elapsed >= max_duration:
                    close_reason = "expired"
                    close_status = "closed_expired"

                if close_reason is not None:
                    closed = await _trader.execute_sell(
                        db=db, trade_id=trade_id,
                        current_price=current_price,
                        reason=close_reason,
                        slippage_bps=slippage_bps,
                        status_override=close_status,
                    )
                    if closed:
                        log.info(
                            "paper_trade_eval_closed",
                            trade_id=trade_id, token_id=token_id,
                            reason=close_reason,
                            current_price=current_price,
                            change_pct=round(change_pct, 2),
                        )
                continue  # skip old cascade entirely for BL-061 rows

            # ---- pre-cutover cascade (existing code, unchanged) ----
            close_reason = None
            if current_price >= tp_price:
                close_reason = "take_profit"
            elif sl_price > 0 and current_price <= sl_price:
                close_reason = "stop_loss"
            elif elapsed >= max_duration:
                close_reason = "expired"
            elif (
                settings.PAPER_TRAILING_ENABLED
                and peak_price is not None
                and peak_pct is not None
                and peak_pct >= settings.PAPER_TRAILING_ACTIVATION_PCT
            ):
                drawdown_threshold = peak_price * (
                    1 - settings.PAPER_TRAILING_DRAWDOWN_PCT / 100.0
                )
                # long_hold positions have sl_price=0 (no SL safety net), so
                # the floor gate would leave them unprotected during giveback.
                # Skip the floor for long_hold; still honor it for normal trades
                # where the regular SL at entry*(1-sl_pct/100) is the fallback.
                is_long_hold = sl_price == 0
                floor_price = entry_price * (
                    1 + settings.PAPER_TRAILING_FLOOR_PCT / 100.0
                )
                if current_price < drawdown_threshold:
                    if is_long_hold or current_price >= floor_price:
                        close_reason = "trailing_stop"
                    else:
                        log.info(
                            "trailing_stop_floor_blocked",
                            trade_id=trade_id,
                            token_id=token_id,
                            peak_pct=round(peak_pct, 2),
                            current_price=current_price,
                            floor_price=round(floor_price, 6),
                            drawdown_threshold=round(drawdown_threshold, 6),
                        )
            if close_reason is not None:
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
                    tp_sell_pct = settings.PAPER_TP_SELL_PCT / 100.0
                    original_amount = float(row[18])
                    original_qty = float(row[19])
                    sell_amount = original_amount * tp_sell_pct
                    keep_amount = original_amount * (1 - tp_sell_pct)

                    await conn.execute(
                        "UPDATE paper_trades SET amount_usd = ?, quantity = ? WHERE id = ? AND status = 'open'",
                        (sell_amount, original_qty * tp_sell_pct, trade_id),
                    )
                    sold = await _trader.execute_sell(
                        db=db,
                        trade_id=trade_id,
                        current_price=current_price,
                        reason=close_reason,
                        slippage_bps=slippage_bps,
                    )
                    if not sold:
                        log.warning("partial_tp_sell_failed", trade_id=trade_id)
                        continue

                    if keep_amount > 0:
                        signal_data_raw = row[14] if len(row) > 14 else "{}"
                        new_id = await _trader.execute_buy(
                            db=db,
                            token_id=token_id,
                            symbol=row[15] if len(row) > 15 else "",
                            name=row[16] if len(row) > 16 else "",
                            chain=row[17] if len(row) > 17 else "coingecko",
                            signal_type="long_hold",
                            signal_data={
                                "origin_trade_id": trade_id,
                                "origin_signal": str(signal_data_raw),
                            },
                            current_price=current_price,
                            amount_usd=keep_amount,
                            tp_pct=100.0,
                            sl_pct=0.0,
                            slippage_bps=0,
                            signal_combo="long_hold",
                        )
                        if new_id is None:
                            log.warning(
                                "long_hold_creation_failed",
                                trade_id=trade_id,
                                token_id=token_id,
                            )
                        else:
                            log.info(
                                "paper_trade_partial_tp",
                                trade_id=trade_id,
                                token_id=token_id,
                                sold_pct=tp_sell_pct * 100,
                                keep_amount=round(keep_amount, 2),
                            )
                    closed = True
                else:
                    closed = await _trader.execute_sell(
                        db=db,
                        trade_id=trade_id,
                        current_price=current_price,
                        reason=close_reason,
                        slippage_bps=slippage_bps,
                    )

                if closed:
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
        except Exception:
            log.exception("trade_eval_row_error", trade_id=trade_id)
            continue

    await conn.commit()
