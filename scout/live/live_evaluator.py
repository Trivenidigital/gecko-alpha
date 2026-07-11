"""Live evaluator — closes open ``live_trades`` on TP / SL / duration (LIVE-02).

Live-mode symmetric twin of :mod:`scout.live.shadow_evaluator`. Same flat
TP/SL/duration exit policy (resolved through the SAME ``LiveConfig`` helpers, so
the live ledger is comparable-by-construction with the shadow soak that gates
it — see ``tasks/plan_pre_live_evidence_gate.md`` §3.2), but where shadow
simulates the exit via ``walk_bids``, the live evaluator places a REAL venue
sell (``adapter.place_exit_order``) for the actual filled quantity and books the
realized fill.

Structure per tick (driven by :func:`live_evaluator_loop`, which first runs
:func:`scout.live.reconciliation.reconcile_open_live_trades`):

1. ``auto_clear_if_expired`` — LIVE-01 parity (un-latch an expired kill).
2. §12a stuck-open watchdog — warn once/day if an open row outlives
   ``max_duration + grace`` (closer not running, or an exit that keeps failing).
3. Scan ``live_trades WHERE status='open'``; for each with a persisted entry
   fill, fetch a fresh price, and on a TP/SL/duration cross place a market sell,
   book realized PnL, and terminalize the row. A non-``filled`` sell fails
   closed to the operator (``needs_manual_review`` + §12b alert). After a close,
   re-check the daily-loss cap (LIVE-04 now unions live PnL).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from scout.db import Database
from scout.live.idempotency import make_exit_client_order_id
from scout.live.kill_switch import KillSwitch, maybe_trigger_from_daily_loss
from scout.live.metrics import inc

if TYPE_CHECKING:  # pragma: no cover
    from scout.config import Settings
    from scout.live.adapter_base import ExchangeAdapter
    from scout.live.config import LiveConfig

log = structlog.get_logger(__name__)

AlertHook = Callable[[str], Awaitable[None]]

# Realistic worst-case for a MARKET sell to reach a terminal state.
EXIT_TIMEOUT_SEC = 30.0

# §12a stuck-open watchdog: an ``open`` live row older than max_duration + this
# grace is stuck — the closer is not running, or its exit keeps failing. Warn
# once/day (module-level dedup; resets on restart, acceptable — the persistent
# watchdog is the follow-up). The live twin of ``shadow_soak_frozen``.
STUCK_OPEN_GRACE_HOURS = 6.0
_last_stuck_open_warn_date: str | None = None


async def _emit_live_alert(
    alert_hook: AlertHook | None, message: str, *, event: str, **fields
) -> None:
    """§12b plain-text operator alert for an automated live-ledger state change.

    Wrapped in the dispatched/delivered/failed log triplet so every fire is
    traceable regardless of delivery. NEVER raises — the DB change has already
    committed. Hookless (tests / paper) = log-only.
    """
    if alert_hook is None:
        return
    log.info("live_ledger_alert_dispatched", alert_event=event, **fields)
    try:
        await alert_hook(message)
        log.info("live_ledger_alert_delivered", alert_event=event, **fields)
    except Exception as exc:  # pragma: no cover — defensive
        log.exception(
            "live_ledger_alert_failed",
            alert_event=event,
            err=str(exc),
            err_type=type(exc).__name__,
            **fields,
        )


async def _maybe_warn_stuck_open(db: Database, config: "LiveConfig") -> None:
    """§12a: warn (once/day) when an ``open`` live row outlives max_duration + grace."""
    global _last_stuck_open_warn_date
    max_dur = config.resolve_max_duration_hours()
    if max_dur is None:
        return
    assert db._conn is not None
    cur = await db._conn.execute(
        "SELECT id, created_at FROM live_trades WHERE status='open' "
        "ORDER BY created_at ASC LIMIT 1"
    )
    row = await cur.fetchone()
    if row is None:
        return
    oldest_dt = datetime.fromisoformat(row[1])
    if oldest_dt.tzinfo is None:
        oldest_dt = oldest_dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    threshold = timedelta(hours=float(max_dur) + STUCK_OPEN_GRACE_HOURS)
    if now - oldest_dt < threshold:
        return
    today = now.strftime("%Y-%m-%d")
    if _last_stuck_open_warn_date == today:
        return
    _last_stuck_open_warn_date = today
    log.warning(
        "live_stuck_open",
        oldest_open_live_trade_id=row[0],
        oldest_created_at=oldest_dt.isoformat(),
        hours_since=round((now - oldest_dt).total_seconds() / 3600.0, 1),
        threshold_hours=float(max_dur) + STUCK_OPEN_GRACE_HOURS,
    )


async def _close_live_trade(
    db: Database,
    trade_id: int,
    new_status: str,
    exit_order_id: str | None,
    exit_fill_price: Decimal,
    realized_pnl_usd: Decimal,
    realized_pnl_pct: Decimal,
) -> None:
    """Stamp terminal close columns inside ``db._txn_lock`` and commit."""
    now_iso = datetime.now(timezone.utc).isoformat()
    assert db._conn is not None
    assert db._txn_lock is not None
    async with db._txn_lock:
        await db._conn.execute(
            "UPDATE live_trades SET status=?, exit_order_id=?, exit_fill_price=?, "
            "realized_pnl_usd=?, realized_pnl_pct=?, closed_at=? WHERE id=?",
            (
                new_status,
                exit_order_id,
                str(exit_fill_price),
                str(realized_pnl_usd),
                str(realized_pnl_pct),
                now_iso,
                trade_id,
            ),
        )
        await db._conn.commit()


async def _mark_exit_needs_review(db: Database, trade_id: int, conf) -> None:
    """LIVE-08: a non-``filled`` sell → ``needs_manual_review`` (fail-closed).

    Records whatever exit metadata the venue returned; books NO realized PnL
    (the position may be wholly or partly held). Stops the row auto-cycling —
    the operator (or the reconciler) finishes it. Avoids an auto-retry loop that
    a deterministic-cid rejected sell or a partial would turn into an oversell.
    """
    assert db._conn is not None
    assert db._txn_lock is not None
    async with db._txn_lock:
        await db._conn.execute(
            "UPDATE live_trades SET status='needs_manual_review', exit_order_id=?, "
            "exit_fill_price=? WHERE id=? AND status='open'",
            (
                conf.venue_order_id,
                str(conf.fill_price) if conf.fill_price is not None else None,
                trade_id,
            ),
        )
        await db._conn.commit()


async def evaluate_open_live_trades(
    *,
    db: Database,
    adapter: "ExchangeAdapter",
    config: "LiveConfig",
    ks: KillSwitch,
    settings: "Settings",
    alert_hook: AlertHook | None = None,
) -> int:
    """Scan + evaluate one pass over open live trades. Returns rows closed."""
    assert db._conn is not None
    # LIVE-01 parity: un-latch an expired kill so it can't outlive killed_until.
    try:
        await ks.auto_clear_if_expired()
    except Exception as exc:  # pragma: no cover — defensive
        log.error("live_kill_auto_clear_failed", error=str(exc))
    # §12a: warn once/day on a stuck-open position.
    try:
        await _maybe_warn_stuck_open(db, config)
    except Exception as exc:  # pragma: no cover — defensive
        log.error("live_stuck_open_check_failed", error=str(exc))

    now = datetime.now(timezone.utc)
    cur = await db._conn.execute(
        "SELECT id, pair, signal_type, size_usd, entry_fill_price, "
        " entry_fill_qty, created_at "
        "FROM live_trades WHERE status='open'"
    )
    rows = await cur.fetchall()
    closed_count = 0

    for (
        trade_id,
        pair,
        signal_type,
        size_s,
        entry_price_s,
        entry_qty_s,
        created_at,
    ) in rows:
        if entry_price_s is None or entry_qty_s is None:
            # The buy filled but the fill wasn't persisted, or the row is an
            # unrecovered orphan — the reconciler owns it. Skip + warn.
            log.warning("live_eval_entry_fill_null_skipped", live_trade_id=trade_id)
            continue
        entry_price = Decimal(entry_price_s)
        entry_qty = Decimal(entry_qty_s)

        try:
            price = await adapter.fetch_price(pair)
        except Exception as exc:
            log.info(
                "live_eval_price_fetch_failed",
                live_trade_id=trade_id,
                error=str(exc),
            )
            continue

        pnl_pct = (price - entry_price) / entry_price * Decimal(100)
        tp = config.resolve_tp_pct()
        sl = config.resolve_sl_pct()
        max_dur = config.resolve_max_duration_hours()

        created_dt = datetime.fromisoformat(created_at)
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)

        new_status: str | None = None
        if tp is not None and pnl_pct >= Decimal(str(tp)):
            new_status = "closed_tp"
        elif sl is not None and pnl_pct <= -Decimal(str(sl)):
            new_status = "closed_sl"
        elif max_dur is not None and (
            now - created_dt >= timedelta(hours=float(max_dur))
        ):
            new_status = "closed_duration"

        if new_status is None:
            continue

        # Place the real market sell for the actual filled quantity. Keyed by a
        # deterministic exit cid so a crash-retry recovers the same order.
        exit_cid = make_exit_client_order_id(trade_id)
        try:
            conf = await adapter.place_exit_order(
                pair=pair,
                base_qty=entry_qty,
                client_order_id=exit_cid,
                timeout_sec=EXIT_TIMEOUT_SEC,
            )
        except Exception as exc:
            # Leave the row open — it is still a held position; the next tick
            # retries (the deterministic cid recovers, no double-sell).
            log.exception(
                "live_exit_place_failed", live_trade_id=trade_id, error=str(exc)
            )
            continue

        if conf.status != "filled" or conf.fill_price is None:
            # LIVE-08: partial / rejected / timeout — fail closed to the operator.
            await _mark_exit_needs_review(db, trade_id, conf)
            await inc(db, "live_exit_review")
            log.warning(
                "live_exit_needs_review",
                live_trade_id=trade_id,
                sell_status=conf.status,
            )
            await _emit_live_alert(
                alert_hook,
                f"live exit needs review: trade #{trade_id} sell status={conf.status}",
                event="live_exit_needs_review",
                live_trade_id=trade_id,
                sell_status=conf.status,
            )
            continue

        exit_price = Decimal(str(conf.fill_price))
        exit_qty = (
            Decimal(str(conf.filled_qty)) if conf.filled_qty is not None else entry_qty
        )
        realized_pnl_usd = exit_price * exit_qty - entry_price * entry_qty
        realized_pnl_pct = (exit_price - entry_price) / entry_price * Decimal(100)

        await _close_live_trade(
            db,
            trade_id,
            new_status,
            conf.venue_order_id,
            exit_price,
            realized_pnl_usd,
            realized_pnl_pct,
        )
        closed_count += 1
        await inc(db, f"live_{new_status}")
        log.info(
            "live_trade_closed",
            live_trade_id=trade_id,
            new_status=new_status,
            entry_fill_price=str(entry_price),
            exit_fill_price=str(exit_price),
            realized_pnl_usd=str(realized_pnl_usd),
            realized_pnl_pct=str(realized_pnl_pct),
        )

        # LIVE-04: daily-loss re-check OUTSIDE the close transaction. The union
        # (kill_switch.py) now counts this live close when LIVE_TRADING_ENABLED.
        try:
            await maybe_trigger_from_daily_loss(db, ks, settings)
        except Exception as exc:
            log.error("live_eval_daily_cap_err", error=str(exc), live_trade_id=trade_id)
            # Swallow — the close itself is already durable.

    return closed_count


async def live_evaluator_loop(
    *,
    db: Database,
    adapter: "ExchangeAdapter",
    config: "LiveConfig",
    ks: KillSwitch,
    settings: "Settings",
    alert_hook: AlertHook | None = None,
    interval_sec: float | None = None,
) -> None:
    """Infinite loop. Reconciles orphans then evaluates each tick. Cancel to stop.

    Runs :func:`scout.live.reconciliation.reconcile_open_live_trades` at the head
    of every tick (periodic reconcile — a mid-run crash is recovered within one
    interval, not only at restart), then :func:`evaluate_open_live_trades`.
    """
    # Imported here (not at module top) to avoid a reconciliation<->evaluator
    # import cycle at module load.
    from scout.live.reconciliation import reconcile_open_live_trades

    sleep_for = (
        interval_sec
        if interval_sec is not None
        else float(getattr(settings, "TRADE_EVAL_INTERVAL_SEC", 60.0))
    )
    log.info("live_evaluator_loop_started", interval_sec=sleep_for)
    while True:
        try:
            await reconcile_open_live_trades(
                db=db,
                adapter=adapter,
                config=config,
                ks=ks,
                settings=settings,
                alert_hook=alert_hook,
            )
            await evaluate_open_live_trades(
                db=db,
                adapter=adapter,
                config=config,
                ks=ks,
                settings=settings,
                alert_hook=alert_hook,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            log.exception("live_evaluator_loop_iteration_failed", error=str(exc))
        await asyncio.sleep(sleep_for)
