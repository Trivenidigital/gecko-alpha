"""Shadow evaluator — closes open ``shadow_trades`` on TP / SL / duration.

Spec §6.2 + §10.5. The loop picks up open rows (plus due-for-review rows
whose ``next_review_at <= now``), fetches a fresh mid price from the adapter,
computes unrealised PnL, and — when TP / SL / duration crosses — walks the
exit side of the book to derive ``exit_walked_vwap`` and realised PnL.

After any close the evaluator calls ``maybe_trigger_from_daily_loss`` **outside
the UPDATE transaction** (spec §6.2): kill-switch writes must never roll back
the close that produced them.

Exception handling (spec §10.1 + §10.5): transient venue errors increment
``review_retries`` and schedule a retry at ``now + 24h``. Three consecutive
failures flip the row to ``needs_manual_review`` and emit a WARN log
(``live_shadow_review_exhausted``).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from scout.db import Database
from scout.live.kill_switch import KillSwitch, maybe_trigger_from_daily_loss
from scout.live.orderbook import walk_bids

if TYPE_CHECKING:  # pragma: no cover
    from scout.config import Settings
    from scout.live.adapter_base import ExchangeAdapter
    from scout.live.config import LiveConfig

log = structlog.get_logger(__name__)

REVIEW_BACKOFF = timedelta(hours=24)
MAX_REVIEW_RETRIES = 3

# §12a (LIVE-01): a shadow soak is "frozen" if the newest shadow_trades row is
# older than this AND no kill is active to explain the silence. 24h matches the
# LIVE-01 validate criterion ("new shadow rows <24h"). This is the thin,
# in-evaluator detection slice; a full freshness watchdog with its own alert
# cadence is a follow-up.
SHADOW_SOAK_FROZEN_HOURS = 24.0

# Once/day dedup for the shadow_soak_frozen warning. Module-level: resets on
# process restart, which is acceptable — worst case one extra warning per
# restart/day (the persistent watchdog is the follow-up). Reset in tests.
_last_shadow_frozen_warn_date: str | None = None


async def _maybe_warn_shadow_soak_frozen(
    db: Database, ks: KillSwitch, settings: "Settings"
) -> None:
    """§12a: warn (once/day) when the shadow soak has silently frozen.

    Fires only in shadow mode when the newest ``shadow_trades`` row is older
    than :data:`SHADOW_SOAK_FROZEN_HOURS` AND no kill is active to explain the
    silence — the exact LIVE-01 failure class (a latched kill froze the soak),
    caught even if the per-tick auto-clear guard were to regress. Structlog
    only; never raises out (callers wrap defensively).
    """
    global _last_shadow_frozen_warn_date
    if getattr(settings, "LIVE_MODE", "paper") != "shadow":
        return
    assert db._conn is not None
    cur = await db._conn.execute("SELECT MAX(created_at) FROM shadow_trades")
    row = await cur.fetchone()
    latest = row[0] if row is not None else None
    if latest is None:
        return  # no shadow trades yet — nothing to be frozen about
    latest_dt = datetime.fromisoformat(latest)
    if latest_dt.tzinfo is None:
        latest_dt = latest_dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if now - latest_dt < timedelta(hours=SHADOW_SOAK_FROZEN_HOURS):
        return  # fresh enough
    if await ks.is_active() is not None:
        return  # a kill explains the silence — expected, not frozen
    today = now.strftime("%Y-%m-%d")
    if _last_shadow_frozen_warn_date == today:
        return  # already warned today
    _last_shadow_frozen_warn_date = today
    log.warning(
        "shadow_soak_frozen",
        newest_shadow_trade_at=latest_dt.isoformat(),
        hours_since=round((now - latest_dt).total_seconds() / 3600.0, 1),
        threshold_hours=SHADOW_SOAK_FROZEN_HOURS,
        kill_active=False,
    )


async def _close_shadow_trade(
    db: Database,
    trade_id: int,
    new_status: str,
    exit_vwap: Decimal | None,
    realized_pnl_usd: Decimal,
    realized_pnl_pct: Decimal,
) -> None:
    """Stamp close columns inside ``db._txn_lock`` and commit."""
    now_iso = datetime.now(timezone.utc).isoformat()
    assert db._conn is not None
    assert db._txn_lock is not None
    async with db._txn_lock:
        await db._conn.execute(
            "UPDATE shadow_trades SET status=?, exit_walked_vwap=?, "
            "realized_pnl_usd=?, realized_pnl_pct=?, closed_at=? WHERE id=?",
            (
                new_status,
                str(exit_vwap) if exit_vwap is not None else None,
                str(realized_pnl_usd),
                str(realized_pnl_pct),
                now_iso,
                trade_id,
            ),
        )
        await db._conn.commit()


async def _bump_review(db: Database, trade_id: int, retries: int) -> None:
    """Increment ``review_retries`` and push ``next_review_at``.

    On the 3rd consecutive failure the row flips to
    ``status='needs_manual_review'`` and emits a WARN log.
    """
    next_at = (datetime.now(timezone.utc) + REVIEW_BACKOFF).isoformat()
    assert db._conn is not None
    assert db._txn_lock is not None
    async with db._txn_lock:
        new_retries = retries + 1
        if new_retries >= MAX_REVIEW_RETRIES:
            await db._conn.execute(
                "UPDATE shadow_trades SET review_retries=?, next_review_at=?, "
                "status='needs_manual_review' WHERE id=?",
                (new_retries, next_at, trade_id),
            )
            log.warning(
                "live_shadow_review_exhausted",
                shadow_trade_id=trade_id,
                review_retries=new_retries,
            )
        else:
            await db._conn.execute(
                "UPDATE shadow_trades SET review_retries=?, next_review_at=? "
                "WHERE id=?",
                (new_retries, next_at, trade_id),
            )
        await db._conn.commit()


async def evaluate_open_shadow_trades(
    *,
    db: Database,
    adapter: "ExchangeAdapter",
    config: "LiveConfig",
    ks: KillSwitch,
    settings: "Settings",
) -> int:
    """Scan + evaluate one pass. Returns the number of rows closed."""
    assert db._conn is not None
    # LIVE-01: clear any expired-but-latched kill BEFORE evaluating, so a
    # daily-loss kill that outlived its killed_until cannot keep Gate 1
    # rejecting every open and freeze the soak. No-op when nothing is
    # active/expired; defensive-wrapped so a clear failure never blocks the
    # evaluation pass.
    try:
        await ks.auto_clear_if_expired()
    except Exception as exc:  # pragma: no cover — defensive
        log.error("shadow_kill_auto_clear_failed", error=str(exc))
    # §12a: once/day warning if the soak has silently frozen.
    try:
        await _maybe_warn_shadow_soak_frozen(db, ks, settings)
    except Exception as exc:  # pragma: no cover — defensive
        log.error("shadow_soak_frozen_check_failed", error=str(exc))
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    cur = await db._conn.execute(
        "SELECT id, pair, signal_type, size_usd, entry_walked_vwap, "
        " review_retries, created_at "
        "FROM shadow_trades "
        "WHERE status='open' "
        "   OR (status='needs_manual_review' "
        "       AND next_review_at IS NOT NULL "
        "       AND next_review_at <= ?)",
        (now_iso,),
    )
    rows = await cur.fetchall()
    closed_count = 0

    for r in rows:
        (
            trade_id,
            pair,
            signal_type,
            size_usd_s,
            entry_s,
            retries,
            created_at,
        ) = r
        if entry_s is None:
            log.warning(
                "live_shadow_entry_vwap_null_skipped",
                shadow_trade_id=trade_id,
            )
            continue
        entry_vwap = Decimal(entry_s)
        size_usd = Decimal(size_usd_s)

        try:
            mid = await adapter.fetch_price(pair)
        except Exception as exc:
            await _bump_review(db, trade_id, retries)
            log.info(
                "live_shadow_eval_transient_error",
                shadow_trade_id=trade_id,
                error=str(exc),
                review_retries=retries + 1,
            )
            continue

        pnl_pct = (mid - entry_vwap) / entry_vwap * Decimal(100)
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

        # Walk the bid side (exit = sell) to realise a vwap.
        try:
            depth = await adapter.fetch_depth(pair)
        except Exception as exc:
            await _bump_review(db, trade_id, retries)
            log.info(
                "live_shadow_exit_fetch_failed",
                shadow_trade_id=trade_id,
                error=str(exc),
                review_retries=retries + 1,
            )
            continue

        qty = size_usd / entry_vwap
        walk = walk_bids(depth, qty)
        if walk.insufficient_liquidity or walk.vwap is None:
            # Fall back to mid so the row can still close — realised PnL is
            # approximate, but leaving it open indefinitely is worse.
            exit_vwap = mid
        else:
            exit_vwap = walk.vwap

        realized_pnl_usd = size_usd * (exit_vwap - entry_vwap) / entry_vwap
        realized_pnl_pct = (exit_vwap - entry_vwap) / entry_vwap * Decimal(100)

        await _close_shadow_trade(
            db,
            trade_id,
            new_status,
            exit_vwap,
            realized_pnl_usd,
            realized_pnl_pct,
        )
        closed_count += 1
        log.info(
            "live_shadow_trade_closed",
            shadow_trade_id=trade_id,
            new_status=new_status,
            entry_walked_vwap=str(entry_vwap),
            exit_walked_vwap=str(exit_vwap),
            realized_pnl_usd=str(realized_pnl_usd),
            realized_pnl_pct=str(realized_pnl_pct),
        )

        # Spec §6.2: kill-switch check lives OUTSIDE the close transaction.
        try:
            await maybe_trigger_from_daily_loss(db, ks, settings)
        except Exception as exc:
            log.error(
                "live_shadow_eval_daily_cap_err",
                error=str(exc),
                shadow_trade_id=trade_id,
            )
            # Swallow — the close itself is already durable.

    return closed_count


async def shadow_evaluator_loop(
    *,
    db: Database,
    adapter: "ExchangeAdapter",
    config: "LiveConfig",
    ks: KillSwitch,
    settings: "Settings",
    interval_sec: float | None = None,
) -> None:
    """Infinite loop. Stop by cancelling the task."""
    sleep_for = (
        interval_sec
        if interval_sec is not None
        else float(getattr(settings, "TRADE_EVAL_INTERVAL_SEC", 60.0))
    )
    while True:
        try:
            await evaluate_open_shadow_trades(
                db=db,
                adapter=adapter,
                config=config,
                ks=ks,
                settings=settings,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            log.error("live_shadow_evaluator_loop_err", error=str(exc))
        await asyncio.sleep(sleep_for)
