"""EVALUATE phase -- paper trade checkpoint tracking with TP/SL/expiry.

Runs every 30 minutes. Uses batch price lookup from price_cache
(single SELECT ... WHERE coin_id IN (...) query).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import structlog

from scout.db import Database
from scout.price_sources import (
    EXIT_PROVENANCE_MARKET,
    EXIT_PROVENANCE_STOP_GAP_MODEL,
    resolve_price_source,
)
from scout.trading.decision_events import emit_trade_decision
from scout.trading.paper import PaperTrader
from scout.trading.params import params_for_signal

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


async def _send_expiry_anomaly_alert(
    session,
    settings,
    *,
    trade_id: int,
    token_id: str,
    signal_type: str,
    exit_reason: str,
    days_held: float,
    headline: str | None = None,
    detail: str | None = None,
) -> None:
    """GA-01 §12b operator alert for force-closes without a usable price.

    A no-price/stale-price expiry close fabricates the recorded PnL
    (entry-price close → exactly $0, or a best-effort stale snapshot).
    The operator must learn the row is unreliable AT WRITE TIME, not via
    a later audit — 12/12 historical `dex:` closes sat unnoticed as $0
    rows diluting auto-suspend stats. Mirrors the auto_suspend
    dispatched/delivered trace pattern; alert failure NEVER breaks the
    close (the DB write already committed before this is called).

    Phase 6 slice 3 generalization: *headline*/*detail* let non-expiry
    forced closes (stale-onset exits) reuse this machinery with accurate
    wording. Defaults preserve the GA-01 expiry-close message verbatim.
    """
    if session is None:
        log.info(
            "trade_expiry_anomaly_alert_skipped_no_session",
            trade_id=trade_id,
            token_id=token_id,
            signal_type=signal_type,
            exit_reason=exit_reason,
        )
        return

    # Deferred import: scout.alerter pulls aiohttp at module level
    # (Windows OpenSSL Applink) — same pattern as auto_suspend.
    from scout import alerter

    body = (
        (headline or "WARNING: paper trade force-closed without a usable market price")
        + "\n"
        f"trade_id: {trade_id}\n"
        f"token_id: {token_id}\n"
        f"signal_type: {signal_type}\n"
        f"exit_reason: {exit_reason}\n"
        f"days_held: {days_held:.1f}\n"
        + (
            detail
            or "Recorded PnL is UNRELIABLE (bookkeeping close, not a market exit)."
        )
    )
    try:
        log.info(
            "trade_expiry_anomaly_alert_dispatched",
            trade_id=trade_id,
            token_id=token_id,
            signal_type=signal_type,
            exit_reason=exit_reason,
        )
        await alerter.send_telegram_message(
            body,
            session,
            settings,
            # parse_mode=None: token_ids and signal names contain `_` / `:`
            # which Telegram MarkdownV1 silently mangles (§12b Class-3).
            parse_mode=None,
            source="trade_expiry_anomaly",
        )
        log.info(
            "trade_expiry_anomaly_alert_delivered",
            trade_id=trade_id,
            token_id=token_id,
            signal_type=signal_type,
            exit_reason=exit_reason,
        )
    except Exception as exc:
        log.exception(
            "trade_expiry_anomaly_alert_failed",
            trade_id=trade_id,
            token_id=token_id,
            signal_type=signal_type,
            exit_reason=exit_reason,
            err=str(exc),
            err_type=type(exc).__name__,
        )


async def _last_observed_liquidity(conn, token_id: str) -> float | None:
    """Last observed liquidity for a token, or None if never observed.

    Phase 6 slice 3 mark provenance: a stale-onset exit records
    ``liquidity_at_exit`` so the ledger can distinguish "exited at onset
    at a plausible mark" from "could not have exited at all" — leaving
    the tracked universe often MEANS liquidity death. NULL is the honest
    answer when the token has no candidates row: exitability could not
    be verified. Prefers the enrichment cron's value
    (``liquidity_usd_enriched``) over the ingest-time snapshot.
    """
    try:
        cur = await conn.execute(
            "SELECT COALESCE(liquidity_usd_enriched, liquidity_usd) "
            "FROM candidates WHERE LOWER(contract_address) = LOWER(?) LIMIT 1",
            (token_id,),
        )
        row = await cur.fetchone()
    except Exception:
        log.exception("stale_onset_liquidity_lookup_failed", token_id=token_id)
        return None
    if row is None or row[0] is None:
        return None
    return float(row[0])


async def evaluate_paper_trades(db: Database, settings, *, session=None) -> None:
    """Check all open paper trades: update checkpoints, check TP/SL, expire old.

    Uses a single batch query to fetch prices for all open trades.
    Logs price_age_seconds alongside the price for each trade.

    *session* (aiohttp.ClientSession | None) powers the GA-01 expiry-anomaly
    operator alert on fabricated force-closes. None (back-compat default)
    skips the alert with a structured log; the close itself is unaffected.
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
                  moonshot_armed_at, conviction_locked_at,
                  checkpoint_1h_pct
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
    # max_duration is now per-signal (Tier 1a). When SIGNAL_PARAMS_ENABLED=False,
    # params_for_signal() returns the global Settings value, so behaviour is
    # unchanged. When the flag is on, each row resolves its own duration.
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
            symbol_row = row[15]
            signal_type_row = row[20]

            # Per-signal params (Tier 1a). Cached for 5 min so this is cheap.
            sp = await params_for_signal(db, signal_type_row, settings)

            # BL-067 conviction-lock overlay. Three gates ALL must pass:
            #   1. Master kill-switch ON (settings.PAPER_CONVICTION_LOCK_ENABLED)
            #   2. Per-signal opt-in (signal_params.conviction_lock_enabled=1)
            #   3. Stack count >= settings.PAPER_CONVICTION_LOCK_THRESHOLD
            #
            # Placement-critical (M2/A2): MUST run BEFORE
            # `max_duration = timedelta(hours=sp.max_duration_hours)` below
            # so the overlaid max_duration_hours flows into the timedelta call.
            #
            # BL-NEW-LOW-PEAK-LOCK (2026-05-11): trail_pct_low_peak NOW
            # overlaid per tasks/findings_sustain_winners_cut_losers_2026_
            # 05_11.md §5 P2. Reverses A3 design decision: empirical data
            # (OSMO #1838 + n=75 cohort) showed the original "low_peak is
            # orthogonal regime, lock shouldn't widen it" rationale was
            # wrong — the bypass was a silent BL-067 contract violation
            # producing 10pp avg giveback on conviction-locked trades that
            # peaked below the low_peak_threshold. Cap at 25%.
            # Leg targets (leg_1_pct/leg_2_pct/qty_frac) STILL NOT overlaid
            # (S6) — BL-067 spec table only widens trail/sl/max_duration.
            #
            # `row[30]` = conviction_locked_at (SELECT extended above).
            conviction_locked_at = row[30] if len(row) > 30 else None

            # PR-review H1: emit one-shot log when master kill is OFF but
            # the trade was previously armed. Operator-rollback scenario
            # (Layer 1 .env flip + restart): trades stay open but their
            # exit gates silently revert from locked to base. Without
            # this log, a fleet of held trades can quietly tighten their
            # trail mid-flight on `PAPER_CONVICTION_LOCK_ENABLED=False`.
            # Idempotent via `conviction_locked_at IS NOT NULL` check —
            # one log per trade per pass; aggregates as a count signal.
            if (
                conviction_locked_at is not None
                and not settings.PAPER_CONVICTION_LOCK_ENABLED
            ):
                log.info(
                    "conviction_lock_disarmed_post_rollback",
                    trade_id=trade_id,
                    token_id=token_id,
                    signal_type=signal_type_row,
                    armed_at=conviction_locked_at,
                    hint="master kill-switch OFF; locked params reverting to base",
                )

            if settings.PAPER_CONVICTION_LOCK_ENABLED and sp.conviction_lock_enabled:
                from scout.trading.conviction import (
                    compute_stack,
                    conviction_locked_params,
                )

                # design-v2 adv-S2: exclude_trade_id prevents the trade
                # from counting itself as a "confirmation" via the
                # paper_trades DISTINCT signal_type scan.
                stack = await compute_stack(
                    db,
                    token_id,
                    str(row[3]),
                    exclude_trade_id=trade_id,
                )
                threshold = settings.PAPER_CONVICTION_LOCK_THRESHOLD
                if stack >= threshold:
                    locked = conviction_locked_params(
                        stack=stack,
                        base={
                            "max_duration_hours": sp.max_duration_hours,
                            "trail_pct": sp.trail_pct,
                            "trail_pct_low_peak": sp.trail_pct_low_peak,
                            "sl_pct": sp.sl_pct,
                        },
                    )
                    # Replace sp with overlaid frozen dataclass. Critical:
                    # this MUST happen before line 158 so the NEW
                    # max_duration_hours is what timedelta() reads.
                    from dataclasses import replace

                    sp = replace(
                        sp,
                        max_duration_hours=locked["max_duration_hours"],
                        trail_pct=locked["trail_pct"],
                        trail_pct_low_peak=locked["trail_pct_low_peak"],
                        sl_pct=locked["sl_pct"],
                    )
                    # D2 idempotency: log + stamp ONCE per trade. Subsequent
                    # passes still apply the overlay (re-derived) but emit
                    # no log. design-v2 D1: also stamp conviction_locked_stack
                    # so dashboard reads stack-at-arm without re-computing.
                    if conviction_locked_at is None:
                        armed_iso = now.isoformat()
                        await conn.execute(
                            "UPDATE paper_trades "
                            "SET conviction_locked_at = ?, "
                            "    conviction_locked_stack = ? "
                            "WHERE id = ?",
                            (armed_iso, stack, trade_id),
                        )
                        await conn.commit()
                        log.info(
                            "conviction_lock_armed",
                            trade_id=trade_id,
                            token_id=token_id,
                            signal_type=signal_type_row,
                            stack=stack,
                            # PR-review N2-arch: explicit bucket value
                            # (saturates at 4) so operator can tell if
                            # the lock applied saturated params or not.
                            bucket=min(stack, 4),
                            threshold=threshold,
                            locked_trail_pct=sp.trail_pct,
                            locked_sl_pct=sp.sl_pct,
                            locked_max_duration_hours=sp.max_duration_hours,
                            armed_at=armed_iso,
                        )

            max_duration = timedelta(hours=sp.max_duration_hours)

            # Compute elapsed up-front so the stale/no-price branches below can
            # still expire trades that have aged past max_duration. Without
            # this, a token whose price_cache stops updating leaves its trade
            # `status='open'` indefinitely (zombie row) — discovered while
            # auditing the BL-064 dashboard mismatch on 2026-04-27.
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
                        # Bookkeeping close at entry_price — no market data.
                        price_provenance="entry_fallback",
                    )
                    log.info(
                        "trade_eval_expired_no_price_forced_close",
                        trade_id=trade_id,
                        token_id=token_id,
                        hours_open=round(elapsed.total_seconds() / 3600, 1),
                    )
                    # GA-01 §12b: the $0 PnL just recorded is fabricated —
                    # tell the operator at write time. Never raises.
                    await _send_expiry_anomaly_alert(
                        session,
                        settings,
                        trade_id=trade_id,
                        token_id=token_id,
                        signal_type=signal_type_row,
                        exit_reason="expired_stale_no_price",
                        days_held=elapsed.total_seconds() / 86400,
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
                        # Best-effort stale snapshot, not a live fill.
                        price_provenance="stale_snapshot",
                    )
                    log.info(
                        "trade_eval_expired_stale_price_forced_close",
                        trade_id=trade_id,
                        token_id=token_id,
                        price_age_seconds=round(price_age_seconds, 1),
                        hours_open=round(elapsed.total_seconds() / 3600, 1),
                    )
                    # GA-01 §12b: exit price is a stale best-effort snapshot,
                    # not a market fill — recorded PnL is unreliable.
                    await _send_expiry_anomaly_alert(
                        session,
                        settings,
                        trade_id=trade_id,
                        token_id=token_id,
                        signal_type=signal_type_row,
                        exit_reason="expired_stale_price",
                        days_held=elapsed.total_seconds() / 86400,
                    )
                    continue

                # Phase 6 slice 3 (operator-approved policy A): stale-onset
                # exit. The price feed for this token has been dead for more
                # than STALE_ONSET_EXIT_HOURS and the trade has NOT reached
                # max_duration. Holding changes nothing — the evaluator
                # skips stale rows, so the only future outcomes are "feed
                # resumes" (rare: leaving the tracked universe usually means
                # liquidity death) or a later fabricated close at the SAME
                # stale mark. Exit NOW at the last-good cached price and
                # record mark provenance so the ledger distinguishes
                # "exited at onset" (liquidity_at_exit observed) from
                # "could not have exited at all" (liquidity_at_exit NULL).
                if price_age_seconds > settings.STALE_ONSET_EXIT_HOURS * 3600:
                    liquidity_at_exit = await _last_observed_liquidity(conn, token_id)
                    closed = await _trader.execute_sell(
                        db=db,
                        trade_id=trade_id,
                        current_price=current_price,
                        reason="stale_onset_exit",
                        # No real fill is happening at a dead feed —
                        # slippage on the stale mark would be fiction.
                        slippage_bps=0,
                        status_override="closed_stale_onset",
                        price_provenance="stale_snapshot",
                    )
                    if closed:
                        await conn.execute(
                            "UPDATE paper_trades SET "
                            "stale_age_seconds_at_exit = ?, "
                            "last_good_price_at = ?, "
                            "liquidity_at_exit = ? "
                            "WHERE id = ?",
                            (
                                round(price_age_seconds, 1),
                                updated_at_str,
                                liquidity_at_exit,
                                trade_id,
                            ),
                        )
                        await conn.commit()
                        log.info(
                            "trade_eval_stale_onset_exit",
                            trade_id=trade_id,
                            token_id=token_id,
                            price_age_seconds=round(price_age_seconds, 1),
                            last_good_price=current_price,
                            last_good_price_at=updated_at_str,
                            liquidity_at_exit=liquidity_at_exit,
                            hours_open=round(elapsed.total_seconds() / 3600, 1),
                        )
                        # §12b: automated close at a non-market mark — the
                        # operator must see it at write time. Never raises.
                        await _send_expiry_anomaly_alert(
                            session,
                            settings,
                            trade_id=trade_id,
                            token_id=token_id,
                            signal_type=signal_type_row,
                            exit_reason="stale_onset_exit",
                            days_held=elapsed.total_seconds() / 86400,
                            headline=(
                                "WARNING: paper trade stale-onset exit "
                                "(price feed stopped updating)"
                            ),
                            detail=(
                                "Exited at the last-good cached price "
                                f"(age {price_age_seconds / 3600:.1f}h; "
                                f"liquidity_at_exit={liquidity_at_exit}). "
                                "Mark is a stale snapshot, not a live fill."
                            ),
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
                # SIG-04 time-death: 1h checkpoint pct (row[31], appended to the
                # SELECT). May be NULL before the 1h checkpoint is recorded.
                cp_1h_pct = row[31] if row[31] is not None else None
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
                        original_trail_drawdown_pct=sp.trail_pct,
                    )
                    if armed_ts is not None:
                        # Use the exact DB-written timestamp — avoids the
                        # microsecond drift of a fresh datetime.now() here.
                        moonshot_armed_at = armed_ts

                # Effective trail width: widest tier wins.
                # - Post-moonshot:    PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT
                # - peak ≥ low-peak threshold: PAPER_LADDER_TRAIL_PCT (full)
                # - peak < threshold: PAPER_LADDER_TRAIL_PCT_LOW_PEAK (tighter)
                # Closes 2026-04-28 strategy review: 91% of trades that peak
                # ≥10% win, but the wide 20% trail meant a +14%-peak fader
                # didn't fire until -6% (negative). The tighter low-peak
                # trail harvests profit on modest peakers without choking
                # moonshots (those go through the moonshot branch above).
                if moonshot_armed_at is not None:
                    if not sp.moonshot_enabled:
                        # BL-NEW-MOONSHOT-OPT-OUT: signal opted out of the
                        # moonshot floor. Use sp.trail_pct directly, letting
                        # calibration / conviction-lock fully control the
                        # trail width. Note: sp.trail_pct here ALREADY
                        # reflects any BL-067 conviction-lock overlay
                        # applied earlier in this evaluator pass — opting
                        # out of moonshot does NOT bypass the lock's
                        # widening effect. See
                        # tasks/findings_moonshot_floor_nullification.md
                        # §3.2 for the conviction-lock interaction matrix.
                        effective_trail_pct = sp.trail_pct
                    else:
                        # BL-067 A1 fix: compose moonshot floor with locked
                        # trail. When conviction-lock has overlaid
                        # sp.trail_pct above (e.g., to 35% at stack=4), the
                        # locked trail wins whenever wider than the
                        # moonshot constant. max() preserves both regimes'
                        # protective intent. Backtest simulator already
                        # used this max() form; production must match.
                        effective_trail_pct = max(
                            settings.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT,
                            sp.trail_pct,
                        )
                elif peak_pct is not None and peak_pct < sp.low_peak_threshold_pct:
                    effective_trail_pct = sp.trail_pct_low_peak
                else:
                    effective_trail_pct = sp.trail_pct

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
                elif leg_1_filled is None and change_pct >= sp.leg_1_pct:
                    await _trader.execute_partial_sell(
                        db=db,
                        trade_id=trade_id,
                        leg=1,
                        sell_qty_frac=sp.leg_1_qty_frac,
                        current_price=current_price,
                        slippage_bps=slippage_bps,
                        price_provenance="market",
                    )
                    continue
                # Leg 2
                elif (
                    leg_1_filled is not None
                    and leg_2_filled is None
                    and change_pct >= sp.leg_2_pct
                ):
                    await _trader.execute_partial_sell(
                        db=db,
                        trade_id=trade_id,
                        leg=2,
                        sell_qty_frac=sp.leg_2_qty_frac,
                        current_price=current_price,
                        slippage_bps=slippage_bps,
                        price_provenance="market",
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
                    and peak_pct >= sp.leg_1_pct
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
                            # BL-NEW-MOONSHOT-OPT-OUT (per #2 PR strategy
                            # reviewer RECOMMEND): emit moonshot_opt_out
                            # field so post-flip operator audit can
                            # distinguish "exited with 30% floor" from
                            # "exited via per-signal trail" without a
                            # signal_params join. Independent of the
                            # exit_status which remains closed_moonshot_trail
                            # (keyed on moonshot_armed_at, not opt-out).
                            log.info(
                                "moonshot_trail_exit",
                                trade_id=trade_id,
                                peak_pct=round(float(peak_pct), 2),
                                exit_pct=round(float(change_pct), 2),
                                give_back_pp=round(
                                    float(peak_pct) - float(change_pct), 2
                                ),
                                moonshot_opt_out=not sp.moonshot_enabled,
                            )
                # BL-NEW-HPF high-peak fade — single-pass tighter exit on
                # confirmed runners (peak_pct >= MIN_PEAK_PCT, retrace >= RETRACE_PCT).
                # MUST be standalone `if close_reason is None`, NOT elif. The
                # trailing_stop branch above enters the elif chain whenever
                # floor_armed AND peak_pct >= leg_1_pct, regardless of whether
                # its inner threshold fires; an elif placement here would be
                # structurally unreachable. See findings_high_peak_giveback.md §13.4.
                #
                # Defer to BL-067 conviction-lock when armed: skipped on
                # conviction_locked_at IS NOT NULL. At stack >= 3, the system
                # has explicitly opted into "let it ride"; honoring that is
                # the contract. See findings_high_peak_giveback.md §7.6.
                if (
                    close_reason is None
                    and settings.PAPER_HIGH_PEAK_FADE_ENABLED
                    and (
                        not settings.PAPER_HIGH_PEAK_FADE_PER_SIGNAL_OPT_IN
                        or sp.high_peak_fade_enabled
                    )
                    and conviction_locked_at is None
                    and peak_pct is not None
                    and peak_pct >= settings.PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT
                    and peak_price is not None
                    and current_price
                    < peak_price
                    * (1 - settings.PAPER_HIGH_PEAK_FADE_RETRACE_PCT / 100.0)
                    and remaining_qty is not None
                    and remaining_qty > 0
                ):
                    fired_at = datetime.now(timezone.utc).isoformat()
                    dry_run = settings.PAPER_HIGH_PEAK_FADE_DRY_RUN
                    retrace_pct_value = (1 - current_price / float(peak_price)) * 100.0
                    await conn.execute(
                        "INSERT OR IGNORE INTO high_peak_fade_audit "
                        "(trade_id, token_id, signal_type, peak_pct, peak_price, "
                        " current_price, threshold_pct, retrace_pct, fired_at, dry_run) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            trade_id,
                            token_id,
                            signal_type_row,
                            float(peak_pct),
                            float(peak_price),
                            float(current_price),
                            float(settings.PAPER_HIGH_PEAK_FADE_MIN_PEAK_PCT),
                            round(retrace_pct_value, 4),
                            fired_at,
                            1 if dry_run else 0,
                        ),
                    )
                    if dry_run:
                        log.info(
                            "high_peak_fade_would_fire",
                            trade_id=trade_id,
                            token_id=token_id,
                            peak_pct=round(float(peak_pct), 2),
                            current_price=current_price,
                            peak_price=float(peak_price),
                            retrace_pp=round(
                                (1 - current_price / float(peak_price)) * 100.0, 2
                            ),
                        )
                    else:
                        close_reason = "high_peak_fade"
                        close_status = "closed_high_peak_fade"
                        log.info(
                            "high_peak_fade_fired",
                            trade_id=trade_id,
                            token_id=token_id,
                            peak_pct=round(float(peak_pct), 2),
                            current_price=current_price,
                            give_back_pp=round(float(peak_pct) - float(change_pct), 2),
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
                # BL-NEW-MOMENTUM-DEATH — dry-run-only sub-peak-fade exit lane.
                # Catches the [MIN_PEAK_PCT, PEAK_FADE_MIN_PEAK_PCT) band that
                # peak_fade structurally cannot reach — its arming floor sits
                # ABOVE where these trades ever traded (§9c: lever exists, data
                # path never reaches it; findings_expired_lane_backtest_2026_07_10.md).
                # Reuses peak_fade's sustained-fade shape (6h AND 24h checkpoints
                # both < RETRACE_RATIO*peak). The `peak_pct < PEAK_FADE_MIN_PEAK_PCT`
                # band guard makes this mutually exclusive with the peak_fade block
                # above, so the two lanes can never double-fire on one trade.
                # DRY_RUN=True (default): NEVER closes — records a would-fire
                # observation only (structured log + trade_decision_events row) so
                # the soak is queryable. The real-close path below is unreachable
                # until a future flip PR sets PAPER_MOMENTUM_DEATH_DRY_RUN=False,
                # mirroring PAPER_HIGH_PEAK_FADE_DRY_RUN above.
                if (
                    close_reason is None
                    and settings.PAPER_MOMENTUM_DEATH_ENABLED
                    and peak_pct is not None
                    and peak_pct >= settings.PAPER_MOMENTUM_DEATH_MIN_PEAK_PCT
                    and peak_pct < settings.PEAK_FADE_MIN_PEAK_PCT
                    and cp_6h_pct is not None
                    and cp_24h_pct is not None
                    and cp_6h_pct < peak_pct * settings.PEAK_FADE_RETRACE_RATIO
                    and cp_24h_pct < peak_pct * settings.PEAK_FADE_RETRACE_RATIO
                    and remaining_qty is not None
                    and remaining_qty > 0
                ):
                    if settings.PAPER_MOMENTUM_DEATH_DRY_RUN:
                        # Fire at most once per trade. The sustained-fade
                        # condition persists on every 30-min eval until expiry,
                        # so an unguarded emit would inflate the soak count by
                        # ~1 row/cycle. Cheapest correct dedup for a dry-run
                        # observation is a table existence check: no schema
                        # change to paper_trades, survives evaluator restarts
                        # (unlike an in-memory set), and only runs when the band
                        # condition is already met (~1 eligible trade/day).
                        cur = await conn.execute(
                            "SELECT 1 FROM trade_decision_events "
                            "WHERE paper_trade_id = ? "
                            "AND reason = 'momentum_death_would_fire' LIMIT 1",
                            (trade_id,),
                        )
                        if await cur.fetchone() is None:
                            log.info(
                                "momentum_death_would_fire",
                                trade_id=trade_id,
                                symbol=symbol_row,
                                signal_type=signal_type_row,
                                peak_pct=round(float(peak_pct), 2),
                                current_pct=round(float(change_pct), 2),
                                dry_run_band=[
                                    settings.PAPER_MOMENTUM_DEATH_MIN_PEAK_PCT,
                                    settings.PEAK_FADE_MIN_PEAK_PCT,
                                ],
                            )
                            # Observability, not control flow — emit_trade_decision
                            # is fail-soft, so a write error never blocks the trade.
                            await emit_trade_decision(
                                db,
                                token_id=token_id,
                                signal_type=signal_type_row or "",
                                decision="observed",
                                reason="momentum_death_would_fire",
                                source_module="scout.trading.evaluator",
                                paper_trade_id=trade_id,
                                event_data={
                                    "peak_pct": round(float(peak_pct), 4),
                                    "current_pct": round(float(change_pct), 4),
                                    "cp_6h_pct": round(float(cp_6h_pct), 4),
                                    "cp_24h_pct": round(float(cp_24h_pct), 4),
                                    "retrace_ratio": settings.PEAK_FADE_RETRACE_RATIO,
                                    "momentum_death_min_peak_pct": (
                                        settings.PAPER_MOMENTUM_DEATH_MIN_PEAK_PCT
                                    ),
                                    "peak_fade_min_peak_pct": (
                                        settings.PEAK_FADE_MIN_PEAK_PCT
                                    ),
                                },
                            )
                    else:
                        close_reason = "momentum_death"
                        close_status = "closed_momentum_death"
                        log.info(
                            "momentum_death_fired",
                            trade_id=trade_id,
                            token_id=token_id,
                            symbol=symbol_row,
                            signal_type=signal_type_row,
                            peak_pct=round(float(peak_pct), 2),
                            current_pct=round(float(change_pct), 2),
                        )
                # SIG-04 absolute time-death — dry-run-only, sub-leg-1
                # flat-at-24h exit lane. Placed ABOVE expiry / BELOW peak_fade +
                # momentum_death, close_reason-None-guarded.
                #
                # DISTINCT BAND (mutual exclusion):
                #   - peak_fade / momentum_death gate on a *sustained fade from a
                #     recorded running peak* (peak_pct >= their floor AND the 6h
                #     AND 24h checkpoints both < RETRACE_RATIO * peak).
                #   - time_death gates on *absolute flatness at 24h for a peak
                #     that never reached leg 1*: leg_1_filled_at IS NULL AND
                #     max(cp_1h,cp_6h,cp_24h) < PAPER_LADDER_LEG_1_PCT AND
                #     checkpoint_24h_pct <= FLAT_PCT AND elapsed >= CHECKPOINT_H.
                # The `leg_1_filled is None` + sub-leg-1 guards target the cohort
                # peak_fade (peak>=10 confirmed runners) never sees. Because all
                # three lanes are `close_reason is None`-guarded and ordered, at
                # most one can ever REAL-close a trade — no double-close. (In
                # DRY_RUN every enabled lane may still *observe* an overlapping
                # trade; each records to its own reason so each soak counts its
                # own cohort. The leg-1 guard keeps time_death silent on the
                # momentum_death cohort, which fills leg 1 to reach the fade.)
                #
                # DRY_RUN=True (default): NEVER closes — records a would-fire
                # observation only (structured log + one trade_decision_events
                # row). The real-close branch below is unreachable until a future
                # flip PR sets PAPER_TIME_DEATH_DRY_RUN=False, mirroring
                # PAPER_MOMENTUM_DEATH_DRY_RUN above.
                if (
                    close_reason is None
                    and settings.PAPER_TIME_DEATH_ENABLED
                    and elapsed
                    >= timedelta(hours=settings.PAPER_TIME_DEATH_CHECKPOINT_H)
                    and cp_24h_pct is not None
                    and cp_24h_pct <= settings.PAPER_TIME_DEATH_FLAT_PCT
                    and leg_1_filled is None
                    and max(
                        v for v in (cp_1h_pct, cp_6h_pct, cp_24h_pct) if v is not None
                    )
                    < settings.PAPER_LADDER_LEG_1_PCT
                ):
                    if settings.PAPER_TIME_DEATH_DRY_RUN:
                        # Fire at most once per trade. The flat-at-24h condition
                        # persists on every 30-min eval until expiry, so an
                        # unguarded emit would inflate the soak count by ~1
                        # row/cycle. Same cheap dedup as momentum_death: a table
                        # existence check on trade_decision_events — no
                        # paper_trades schema change, survives evaluator
                        # restarts, only runs when the band is already met.
                        cur = await conn.execute(
                            "SELECT 1 FROM trade_decision_events "
                            "WHERE paper_trade_id = ? "
                            "AND reason = 'time_death_would_fire' LIMIT 1",
                            (trade_id,),
                        )
                        if await cur.fetchone() is None:
                            log.info(
                                "time_death_would_fire",
                                trade_id=trade_id,
                                symbol=symbol_row,
                                signal_type=signal_type_row,
                                peak_pct=(
                                    round(float(peak_pct), 2)
                                    if peak_pct is not None
                                    else None
                                ),
                                current_pct=round(float(change_pct), 2),
                                cp_24h_pct=round(float(cp_24h_pct), 2),
                                elapsed_h=round(elapsed.total_seconds() / 3600.0, 1),
                            )
                            # Observability, not control flow — emit_trade_decision
                            # is fail-soft, so a write error never blocks the trade.
                            await emit_trade_decision(
                                db,
                                token_id=token_id,
                                signal_type=signal_type_row or "",
                                decision="observed",
                                reason="time_death_would_fire",
                                source_module="scout.trading.evaluator",
                                paper_trade_id=trade_id,
                                event_data={
                                    "peak_pct": (
                                        round(float(peak_pct), 4)
                                        if peak_pct is not None
                                        else None
                                    ),
                                    "current_pct": round(float(change_pct), 4),
                                    "cp_1h_pct": (
                                        round(float(cp_1h_pct), 4)
                                        if cp_1h_pct is not None
                                        else None
                                    ),
                                    "cp_6h_pct": (
                                        round(float(cp_6h_pct), 4)
                                        if cp_6h_pct is not None
                                        else None
                                    ),
                                    "cp_24h_pct": round(float(cp_24h_pct), 4),
                                    "flat_pct": settings.PAPER_TIME_DEATH_FLAT_PCT,
                                    "checkpoint_h": (
                                        settings.PAPER_TIME_DEATH_CHECKPOINT_H
                                    ),
                                    "ladder_leg_1_pct": (
                                        settings.PAPER_LADDER_LEG_1_PCT
                                    ),
                                },
                            )
                    else:
                        close_reason = "time_death"
                        close_status = "closed_time_death"
                        log.info(
                            "time_death_fired",
                            trade_id=trade_id,
                            token_id=token_id,
                            symbol=symbol_row,
                            signal_type=signal_type_row,
                            cp_24h_pct=round(float(cp_24h_pct), 2),
                            elapsed_h=round(elapsed.total_seconds() / 3600.0, 1),
                        )
                # Expiry — last resort
                if close_reason is None and elapsed >= max_duration:
                    close_reason = "expired"
                    close_status = "closed_expired"

                if close_reason is not None:
                    # SIG-05 stop-fill realism. A fresh price (age <= 3600) can
                    # still be an arbitrarily-deep crash snapshot: between 30-min
                    # eval cycles a token gaps far below the stop, so booking the
                    # "fill" at current_price records the crash, not the stop
                    # (measured: -28.1% avg on a -10% config). When the model is
                    # on, a stop close books at max(current_price, sl_price*(1 -
                    # gap)) — near the stop with a bounded gap allowance. Only
                    # 'stop_loss' is re-priced; every other close path keeps the
                    # observed price + 'market' provenance. Fail-closed: default
                    # off preserves the exact pre-existing fill.
                    fill_price = current_price
                    fill_provenance = EXIT_PROVENANCE_MARKET
                    if (
                        close_reason == "stop_loss"
                        and settings.PAPER_STOP_FILL_SLIPPAGE_MODEL
                        and sl_price > 0
                    ):
                        gap_floor = sl_price * (
                            1 - settings.PAPER_STOP_GAP_BPS / 10000.0
                        )
                        modeled_fill = max(current_price, gap_floor)
                        if modeled_fill > current_price:
                            # The observed price gapped below the bounded gap
                            # floor — clamp the fill and record the raw observed
                            # price so realized-vs-modeled stays auditable.
                            fill_price = modeled_fill
                            fill_provenance = EXIT_PROVENANCE_STOP_GAP_MODEL
                            log.info(
                                "stop_fill_modeled",
                                trade_id=trade_id,
                                token_id=token_id,
                                raw_observed_price=current_price,
                                modeled_fill_price=modeled_fill,
                                sl_price=sl_price,
                                gap_bps=settings.PAPER_STOP_GAP_BPS,
                            )
                            await emit_trade_decision(
                                db,
                                token_id=token_id,
                                signal_type=signal_type_row or "",
                                decision="stop_fill_modeled",
                                reason="stop_fill_slippage_model",
                                source_module="scout.trading.evaluator",
                                paper_trade_id=trade_id,
                                event_data={
                                    "raw_observed_price": current_price,
                                    "modeled_fill_price": modeled_fill,
                                    "sl_price": sl_price,
                                    "gap_bps": settings.PAPER_STOP_GAP_BPS,
                                },
                            )
                    closed = await _trader.execute_sell(
                        db=db,
                        trade_id=trade_id,
                        current_price=fill_price,
                        reason=close_reason,
                        slippage_bps=slippage_bps,
                        status_override=close_status,
                        price_provenance=fill_provenance,
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
                        price_provenance="market",
                    )
                    if not sold:
                        log.warning("partial_tp_sell_failed", trade_id=trade_id)
                        continue

                    if keep_amount > 0:
                        signal_data_raw = row[14] if len(row) > 14 else "{}"
                        # PR-review NIT (BL-NEW-LIVE-ELIGIBLE): settings
                        # intentionally omitted — long_hold is a partial-TP
                        # carry, NOT a tier-eligible signal. would_be_live
                        # stamps 0 by definition. Re-evaluate if any future
                        # tier rule includes signal_type='long_hold'.
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
                            # This branch is only reachable with a FRESH
                            # price_map hit for token_id, so a price_cache
                            # row demonstrably exists right now.
                            price_source=resolve_price_source(token_id, True),
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
                        price_provenance="market",
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
