"""Boot-time reconciliation (spec §10.5).

``reconcile_open_shadow_trades``: scans open shadow rows, closes any that
crossed TP / SL / duration during downtime as
``status='closed_via_reconciliation'``. ALWAYS logs
``live_boot_reconciliation_done`` on exit — including with
``rows_inspected=0`` — so operators can grep for the terminal log and
confirm the engine came up clean (T3: absence of log != success).

``emit_live_startup_status``: one-shot summary of subsystem health. Distinct
from ``_done`` above; this grep target rolls up kill state, open-shadow
count, and a cheap Binance liveness probe.

Schema note (spec §3.1): the ``shadow_trades`` CHECK constraint only permits
``closed_via_reconciliation`` as the single reconciled-close status. The
crossed reason (TP / SL / duration) is captured in the ``crossed_reason``
field of the WARN log, not in the status column.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

import structlog

from scout.db import Database
from scout.live.kill_switch import KillSwitch

if TYPE_CHECKING:  # pragma: no cover
    from scout.config import Settings
    from scout.live.adapter_base import ExchangeAdapter
    from scout.live.config import LiveConfig

log = structlog.get_logger(__name__)


async def _close_crossed_row(
    db: Database,
    trade_id: int,
    mid: Decimal,
    entry_vwap: Decimal,
    size_usd: Decimal,
) -> None:
    """Close a shadow row found crossed during boot reconciliation.

    Uses ``mid`` as the exit vwap — this is the fast recovery path so we do
    NOT walk the order book. Realised PnL is approximate but better than
    leaving a crossed row open until the next scheduled evaluator tick.
    """
    realized_pnl_usd = size_usd * (mid - entry_vwap) / entry_vwap
    realized_pnl_pct = (mid - entry_vwap) / entry_vwap * Decimal(100)
    now_iso = datetime.now(timezone.utc).isoformat()
    assert db._conn is not None
    assert db._txn_lock is not None
    async with db._txn_lock:
        await db._conn.execute(
            "UPDATE shadow_trades SET status='closed_via_reconciliation', "
            "exit_walked_vwap=?, realized_pnl_usd=?, realized_pnl_pct=?, "
            "closed_at=? WHERE id=?",
            (
                str(mid),
                str(realized_pnl_usd),
                str(realized_pnl_pct),
                now_iso,
                trade_id,
            ),
        )
        await db._conn.commit()


async def reconcile_open_shadow_trades(
    *,
    db: Database,
    adapter: "ExchangeAdapter",
    config: "LiveConfig",
    ks: KillSwitch,
    settings: "Settings",
) -> None:
    """Boot-time recovery pass.

    1. Emit ``live_boot_reconciliation_drift_window`` with ``restart_at`` and
       the oldest ``created_at`` among open rows (``None`` when no open rows).
    2. For each open row: fetch mid, compute ``pnl_pct``, and close as
       ``closed_via_reconciliation`` when TP / SL / duration has crossed.
       The crossed kind is reported in the WARN log's ``crossed_reason``
       field (``tp_crossed`` | ``sl_crossed`` | ``duration_crossed``), NOT
       the status column — the CHECK constraint forbids anything else.
    3. ALWAYS emit ``live_boot_reconciliation_done`` before returning —
       including when ``rows_inspected=0``.
    4. ``fetch_price`` failures on a single row log
       ``live_boot_reconciliation_row_err`` and continue to the next row;
       the row stays open. Never throws.
    """
    assert db._conn is not None
    restart_at = datetime.now(timezone.utc)

    cur = await db._conn.execute(
        "SELECT MIN(created_at) FROM shadow_trades WHERE status='open'"
    )
    earliest_row = await cur.fetchone()
    earliest = earliest_row[0] if earliest_row is not None else None
    log.info(
        "live_boot_reconciliation_drift_window",
        restart_at=restart_at.isoformat(),
        earliest_open_created_at=earliest,
    )

    cur = await db._conn.execute(
        "SELECT id, pair, signal_type, size_usd, entry_walked_vwap, created_at "
        "FROM shadow_trades WHERE status='open'"
    )
    rows = await cur.fetchall()

    rows_inspected = 0
    rows_closed = 0
    rows_resumed = 0

    for trade_id, pair, signal_type, size_s, entry_s, created_at in rows:
        rows_inspected += 1

        if entry_s is None:
            log.warning(
                "live_boot_reconciliation_skipped_null_vwap",
                shadow_trade_id=trade_id,
            )
            rows_resumed += 1
            continue

        entry_vwap = Decimal(entry_s)
        size_usd = Decimal(size_s)

        try:
            mid = await adapter.fetch_price(pair)
        except Exception as exc:
            log.error(
                "live_boot_reconciliation_row_err",
                shadow_trade_id=trade_id,
                error=str(exc),
            )
            rows_resumed += 1
            continue

        pnl_pct = (mid - entry_vwap) / entry_vwap * Decimal(100)
        tp = config.resolve_tp_pct()
        sl = config.resolve_sl_pct()
        max_dur = config.resolve_max_duration_hours()

        created_dt = datetime.fromisoformat(created_at)
        if created_dt.tzinfo is None:
            created_dt = created_dt.replace(tzinfo=timezone.utc)

        crossed_kind: str | None = None
        if tp is not None and pnl_pct >= Decimal(str(tp)):
            crossed_kind = "tp"
        elif sl is not None and pnl_pct <= -Decimal(str(sl)):
            crossed_kind = "sl"
        elif max_dur is not None and (
            restart_at - created_dt >= timedelta(hours=float(max_dur))
        ):
            crossed_kind = "duration"

        if crossed_kind is not None:
            await _close_crossed_row(db, trade_id, mid, entry_vwap, size_usd)
            rows_closed += 1
            log.warning(
                "live_boot_reconciliation_closed",
                shadow_trade_id=trade_id,
                crossed_reason=f"{crossed_kind}_crossed",
                mid=str(mid),
                entry_walked_vwap=str(entry_vwap),
                pnl_pct=str(pnl_pct),
            )
        else:
            rows_resumed += 1

    log.info(
        "live_boot_reconciliation_done",
        rows_inspected=rows_inspected,
        rows_closed=rows_closed,
        rows_resumed=rows_resumed,
    )


async def emit_live_startup_status(
    *,
    db: Database,
    adapter: "ExchangeAdapter",
    config: "LiveConfig",
    ks: KillSwitch,
) -> None:
    """Emit a single ``live_startup_status`` event after reconciliation.

    Fields: ``live_mode``, ``active_kill_event_id``, ``shadow_trades_open``,
    ``binance_reachable``. The Binance probe is a best-effort 5s ticker
    fetch; a failure is recorded (``binance_reachable=False``) but does NOT
    raise so the boot sequence can continue.
    """
    assert db._conn is not None
    active = await ks.is_active()
    active_id = active.kill_event_id if active is not None else None

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM shadow_trades WHERE status='open'"
    )
    shadow_open = (await cur.fetchone())[0]

    binance_reachable = True
    try:
        await asyncio.wait_for(adapter.fetch_price("BTCUSDT"), timeout=5.0)
    except Exception:
        binance_reachable = False

    log.info(
        "live_startup_status",
        live_mode=config.mode,
        active_kill_event_id=active_id,
        shadow_trades_open=shadow_open,
        binance_reachable=binance_reachable,
    )
