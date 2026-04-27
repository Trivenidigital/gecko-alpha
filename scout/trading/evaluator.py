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


async def _load_bl062_cutover_ts(conn) -> str | None:
    """Load BL-062 peak-fade cutover timestamp from paper_migrations.

    Returns None if the row is missing. Callers treat None as 'no
    cutover recorded' and should not use this value to filter fires —
    it exists for the 30-day calibration review query.
    """
    cur = await conn.execute(
        "SELECT cutover_ts FROM paper_migrations WHERE name = 'bl062_peak_fade'"
    )
    row = await cur.fetchone()
    return row[0] if row else None


def compute_trail_threshold(peak_price: float, drawdown_pct: float) -> float:
    """Pure helper: trailing-stop trigger price for a given peak + drawdown.

    Extracted so the formula can be exercised by deterministic table-driven
    tests independent of the evaluator state machine.

    Invariants (asserted by tests):
    - Result is always > 0 when peak > 0 and 0 < drawdown < 100.
    - Result is always < peak_price (trail must trigger below peak).
    - Widening drawdown monotonically lowers the threshold.
    """
    return peak_price * (1 - drawdown_pct / 100.0)


def _parse_ts(s: str | None) -> datetime | None:
    """Parse SQLite datetime('now') (space, no tz) or ISO-with-tz into a UTC datetime.

    paper_trades.created_at uses SQLite's default space-separated, tz-less
    format; paper_migrations.cutover_ts uses Python's isoformat with +00:00.
    Both need to compare apples-to-apples — normalize via fromisoformat and
    attach UTC when naive.
    """
    if s is None:
        return None
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


async def evaluate_paper_trades(db: Database, settings) -> None:
    """Check all open paper trades: update checkpoints, check TP/SL, expire old.

    Uses a single batch query to fetch prices for all open trades.
    Logs price_age_seconds alongside the price for each trade.
    """
    conn = db._conn
    if conn is None:
        raise RuntimeError("Database not initialized.")

    # 1. Get all open trades. Rows are unpacked positionally; column order
    # below maps to row[0]..row[29]. `original_trail_drawdown_pct` is
    # intentionally NOT selected — it is written at arm time for post-mortem
    # queries on closed trades but is never consumed on the evaluator hot
    # path; pulling it here would just bloat each pass.
    cursor = await conn.execute("""SELECT id, token_id, entry_price, opened_at,
                  tp_price, sl_price, tp_pct, sl_pct,
                  checkpoint_1h_price, checkpoint_6h_price,
                  checkpoint_24h_price, checkpoint_48h_price,
                  peak_price, peak_pct, signal_data, symbol, name, chain,
                  amount_usd, quantity, signal_type,
                  created_at, leg_1_filled_at, leg_2_filled_at,
                  remaining_qty, floor_armed, realized_pnl_usd,
                  checkpoint_6h_pct, checkpoint_24h_pct,
                  moonshot_armed_at
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
    # BL-062 peak-fade is gated by is_bl061 + PEAK_FADE_ENABLED only; its
    # own cutover row exists solely for the 30-day review query (see
    # _load_bl062_cutover_ts), so we intentionally do NOT load it here.

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

            # Compute elapsed up-front so the stale/no-price branches below can
            # still expire trades that have aged past max_duration. Without
            # this, a token whose price_cache stops updating leaves its trade
            # `status='open'` indefinitely (zombie row) — discovered while
            # auditing the BL-064 dashboard mismatch on 2026-04-27.
            #
            # `max_duration` is a `timedelta` (built from PAPER_MAX_DURATION_HOURS
            # at line 134); `elapsed` is `now - opened_at` (also timedelta), so
            # the comparisons below are unit-consistent.
            elapsed = now - opened_at

            price_data = price_map.get(token_id)
            if price_data is None:
                if elapsed >= max_duration:
                    # No price at all but trade is past expiry — force close
                    # at entry_price for a zero-PnL marker. We don't know
                    # the token's current market price (it dropped from
                    # price_cache entirely), so the conservative move is
                    # entry-price → pnl_pct=0 with a distinct exit_reason.
                    #
                    # `slippage_bps=0` because no real fill is happening — this
                    # is a bookkeeping close, not a market sell. `status_override`
                    # pre-existed on `execute_sell`; reused here so combo_performance
                    # rollups (CLOSED_COUNTABLE_STATUSES) treat this identically
                    # to a clean `closed_expired`.
                    await _trader.execute_sell(
                        db=db,
                        trade_id=trade_id,
                        current_price=entry_price,
                        reason="expired_stale_no_price",
                        slippage_bps=0,
                        status_override="closed_expired",
                    )
                    log.info(
                        "trade_eval_expired_no_price_forced_close",
                        trade_id=trade_id,
                        token_id=token_id,
                        hours_open=round(elapsed.total_seconds() / 3600, 1),
                    )
                    continue
                log.info("trade_eval_no_price", trade_id=trade_id, token_id=token_id)
                continue

            current_price, updated_at_str = price_data
            updated_at = datetime.fromisoformat(updated_at_str).replace(
                tzinfo=timezone.utc
            )
            price_age_seconds = (now - updated_at).total_seconds()

            if price_age_seconds > 3600:  # 1 hour max for evaluator
                if elapsed >= max_duration:
                    # Stale price but trade is past expiry — close at the
                    # stale snapshot (best-effort) with a distinct
                    # exit_reason so analytics can distinguish from a clean
                    # `closed_expired`. Better than letting the row sit
                    # `open` forever. See no-price branch above for the
                    # rationale on `slippage_bps=0` and `status_override`.
                    await _trader.execute_sell(
                        db=db,
                        trade_id=trade_id,
                        current_price=current_price,
                        reason="expired_stale_price",
                        slippage_bps=0,
                        status_override="closed_expired",
                    )
                    log.info(
                        "trade_eval_expired_stale_price_forced_close",
                        trade_id=trade_id,
                        token_id=token_id,
                        price_age_seconds=round(price_age_seconds, 1),
                        hours_open=round(elapsed.total_seconds() / 3600, 1),
                    )
                    continue
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

            # `elapsed` was hoisted to before the stale/no-price guards above
            # so the zombie-expiry branches can use it; it's still valid here.
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
            remaining_qty = (
                float(row[24]) if len(row) > 24 and row[24] is not None else None
            )
            floor_armed = (
                bool(row[25]) if len(row) > 25 and row[25] is not None else False
            )

            # BL-061 eligibility via datetime compare: SQLite datetime('now')
            # has second precision; cutover_ts has microsecond. Truncate cutover
            # so trades inserted in the same second as the migration are included.
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
                # BL-062 peak-fade checkpoint pct values (may be NULL)
                cp_6h_pct = row[27] if row[27] is not None else None
                cp_24h_pct = row[28] if row[28] is not None else None
                # BL-063 moonshot state (NULL on pre-cutover or not-yet-armed
                # rows). Indexed directly — schema migration guarantees the
                # column exists; a `len(row)` guard would silently mask schema
                # drift as "unarmed forever".
                moonshot_armed_at = row[29]

                # BL-063 moonshot arm — fires once when peak_pct crosses the
                # threshold. Atomic UPDATE inside arm_moonshot guards against
                # concurrent ticks. Order matters: arm BEFORE the trail-stop
                # check below, so the same eval pass that crosses the threshold
                # uses the widened trail. Note: `tp_disabled` from the spec
                # was unnecessary because BL-061 ladder rows reach the
                # `continue` at the end of this block before the legacy
                # cascade ever consults `tp_price` — the fixed-TP exit path
                # is structurally unreachable for moonshot-eligible trades.
                if (
                    settings.PAPER_MOONSHOT_ENABLED
                    and moonshot_armed_at is None
                    and peak_pct is not None
                    and peak_pct >= settings.PAPER_MOONSHOT_THRESHOLD_PCT
                ):
                    armed_ts = await _trader.arm_moonshot(
                        db=db,
                        trade_id=trade_id,
                        peak_pct_at_arm=float(peak_pct),
                        original_trail_drawdown_pct=settings.PAPER_LADDER_TRAIL_PCT,
                    )
                    if armed_ts is not None:
                        # Use the exact DB-written timestamp — avoids the
                        # microsecond drift of a fresh datetime.now() here.
                        moonshot_armed_at = armed_ts

                # Effective trail width for this trade: widened if moonshot armed.
                effective_trail_pct = (
                    settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT
                    if moonshot_armed_at is not None
                    else settings.PAPER_LADDER_TRAIL_PCT
                )

                close_reason = None
                close_status: str | None = None
                # BL-061 ladder exit cascade — order is load-bearing and
                # locked in by tests/test_moonshot_exit.py:
                #   SL  →  Leg 1  →  Leg 2  →  Floor  →  Trailing stop
                #   (then peak-fade, then expiry — both gated by
                #    `if close_reason is None` further down).
                # Editing this elif chain breaks the regression tests
                # `test_floor_exit_pre_empts_moonshot_trail` and
                # `test_moonshot_trail_wins_over_peak_fade`.
                # SL applies only before leg 1 arms the floor
                if not floor_armed and sl_price > 0 and current_price <= sl_price:
                    close_reason = "stop_loss"
                    close_status = "closed_sl"
                # Leg 1
                elif (
                    leg_1_filled is None
                    and change_pct >= settings.PAPER_LADDER_LEG_1_PCT
                ):
                    await _trader.execute_partial_sell(
                        db=db,
                        trade_id=trade_id,
                        leg=1,
                        sell_qty_frac=settings.PAPER_LADDER_LEG_1_QTY_FRAC,
                        current_price=current_price,
                        slippage_bps=slippage_bps,
                    )
                    continue
                # Leg 2
                elif (
                    leg_1_filled is not None
                    and leg_2_filled is None
                    and change_pct >= settings.PAPER_LADDER_LEG_2_PCT
                ):
                    await _trader.execute_partial_sell(
                        db=db,
                        trade_id=trade_id,
                        leg=2,
                        sell_qty_frac=settings.PAPER_LADDER_LEG_2_QTY_FRAC,
                        current_price=current_price,
                        slippage_bps=slippage_bps,
                    )
                    continue
                # Floor exit — once armed, don't let the runner slice close below entry
                elif floor_armed and current_price <= entry_price:
                    close_reason = "floor"
                    close_status = "closed_floor"
                    log.info(
                        "floor_exit",
                        trade_id=trade_id,
                        peak_pct=round(peak_pct or 0, 2),
                        current_price=current_price,
                    )
                # Trailing stop on runner (post-leg-1 only). Width is
                # widened to PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT once moonshot
                # is armed; close status reflects that path so dashboards
                # and digests can attribute the source.
                elif (
                    floor_armed
                    and peak_price is not None
                    and peak_pct is not None
                    and peak_pct >= settings.PAPER_LADDER_LEG_1_PCT
                ):
                    trail_threshold = compute_trail_threshold(
                        peak_price, effective_trail_pct
                    )
                    if current_price < trail_threshold:
                        close_reason = "trailing_stop"
                        close_status = (
                            "closed_moonshot_trail"
                            if moonshot_armed_at is not None
                            else "closed_trailing_stop"
                        )
                        if moonshot_armed_at is not None:
                            # give_back_pp = peak_pct - exit_pct, in
                            # percentage points (NOT a fraction of peak).
                            # E.g. peak 60%, exit 35% → give_back_pp 25.
                            log.info(
                                "moonshot_trail_exit",
                                trade_id=trade_id,
                                peak_pct=round(float(peak_pct), 2),
                                exit_pct=round(float(change_pct), 2),
                                give_back_pp=round(
                                    float(peak_pct) - float(change_pct), 2
                                ),
                            )
                # BL-062 peak-fade — sustained fade at 6h AND 24h checkpoints.
                # NB: fires on any pass with cp_24h present, not only the pass
                # that records it. The expiry bound caps the window to 1-2
                # eval cycles in practice.
                if (
                    close_reason is None
                    and settings.PEAK_FADE_ENABLED
                    and peak_pct is not None
                    and peak_pct >= settings.PEAK_FADE_MIN_PEAK_PCT
                    and cp_6h_pct is not None
                    and cp_24h_pct is not None
                    and cp_6h_pct < peak_pct * settings.PEAK_FADE_RETRACE_RATIO
                    and cp_24h_pct < peak_pct * settings.PEAK_FADE_RETRACE_RATIO
                    and remaining_qty is not None
                    and remaining_qty > 0
                ):
                    close_reason = "peak_fade"
                    close_status = "closed_peak_fade"
                    await conn.execute(
                        "UPDATE paper_trades SET peak_fade_fired_at = ? WHERE id = ?",
                        (datetime.now(timezone.utc).isoformat(), trade_id),
                    )
                # Expiry — last resort
                if close_reason is None and elapsed >= max_duration:
                    close_reason = "expired"
                    close_status = "closed_expired"

                if close_reason is not None:
                    closed = await _trader.execute_sell(
                        db=db,
                        trade_id=trade_id,
                        current_price=current_price,
                        reason=close_reason,
                        slippage_bps=slippage_bps,
                        status_override=close_status,
                    )
                    if closed:
                        log.info(
                            "paper_trade_eval_closed",
                            trade_id=trade_id,
                            token_id=token_id,
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
