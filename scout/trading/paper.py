"""PaperTrader -- simulates trade execution by logging to DB at current price."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

from scout.db import Database

if TYPE_CHECKING:
    from scout.live.engine import LiveEngine

log = structlog.get_logger()


CLOSED_COUNTABLE_STATUSES: tuple[str, ...] = (
    "closed_tp",
    "closed_sl",
    "closed_expired",
    "closed_trailing_stop",
)


@dataclass
class _PaperTradeHandoff:
    """Minimal handoff payload passed to LiveEngine.on_paper_trade_opened."""

    id: int
    signal_type: str
    symbol: str
    coin_id: str


class PaperTrader:
    """Simulates trade execution with slippage simulation."""

    def __init__(self, *, live_engine: "LiveEngine | None" = None) -> None:
        self._live_engine = live_engine
        self._pending_live_tasks: set[asyncio.Task] = set()

    async def execute_buy(
        self,
        db: Database,
        token_id: str,
        symbol: str,
        name: str,
        chain: str,
        signal_type: str,
        signal_data: dict,
        current_price: float,
        amount_usd: float,
        tp_pct: float,
        sl_pct: float,
        slippage_bps: int = 0,
        *,
        signal_combo: str,
        lead_time_vs_trending_min: float | None = None,
        lead_time_vs_trending_status: str | None = None,
    ) -> int | None:
        """Record a paper buy. Returns trade ID, or None if rejected by guards.

        Applies slippage to entry price: effective_entry = price * (1 + bps/10000).
        sl_pct is positive: sl_price = entry * (1 - sl_pct/100).
        """
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        effective_entry = current_price * (1 + slippage_bps / 10000)
        if effective_entry <= 0:
            log.warning(
                "paper_trade_zero_price", token_id=token_id, current_price=current_price
            )
            return None
        quantity = amount_usd / effective_entry
        # Sanity check: quantity must be positive and finite
        if quantity <= 0 or not (quantity == quantity):  # NaN check
            log.warning(
                "paper_trade_invalid_quantity", token_id=token_id, quantity=quantity
            )
            return None
        tp_price = effective_entry * (1 + tp_pct / 100)
        sl_price = effective_entry * (1 - sl_pct / 100) if sl_pct > 0 else 0.0
        now = datetime.now(timezone.utc).isoformat()

        INSERT_SQL = """
INSERT INTO paper_trades
  (token_id, symbol, name, chain, signal_type, signal_data,
   entry_price, amount_usd, quantity,
   tp_pct, sl_pct, tp_price, sl_price,
   status, opened_at,
   signal_combo, lead_time_vs_trending_min, lead_time_vs_trending_status,
   remaining_qty, floor_armed, realized_pnl_usd)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?,
        ?, 0, 0.0)
"""
        cursor = await conn.execute(
            INSERT_SQL,
            (
                token_id,
                symbol,
                name,
                chain,
                signal_type,
                json.dumps(signal_data),
                effective_entry,
                amount_usd,
                quantity,
                tp_pct,
                sl_pct,
                tp_price,
                sl_price,
                now,
                signal_combo,
                lead_time_vs_trending_min,
                lead_time_vs_trending_status,
                quantity,  # remaining_qty = full qty at open
            ),
        )
        trade_id = cursor.lastrowid
        await conn.commit()

        log.info(
            "paper_trade_opened",
            trade_id=trade_id,
            token_id=token_id,
            symbol=symbol,
            signal_type=signal_type,
            entry_price=effective_entry,
            amount_usd=amount_usd,
            tp_price=tp_price,
            sl_price=sl_price,
        )

        # BL-055 chokepoint: fire-and-forget handoff to LiveEngine when injected
        # and the signal is allowlisted. Never await — paper trade flow must not
        # block on live dispatch.
        if (
            trade_id is not None
            and self._live_engine is not None
            and self._live_engine.is_eligible(signal_type)
        ):
            if len(self._pending_live_tasks) > 50:
                log.warning(
                    "live_handoff_backpressure",
                    pending=len(self._pending_live_tasks),
                    trade_id=trade_id,
                )
            task = asyncio.create_task(
                self._live_engine.on_paper_trade_opened(
                    _PaperTradeHandoff(
                        id=trade_id,
                        signal_type=signal_type,
                        symbol=symbol,
                        coin_id=token_id,
                    )
                )
            )
            self._pending_live_tasks.add(task)
            task.add_done_callback(self._pending_live_tasks.discard)

        return trade_id

    async def execute_partial_sell(
        self,
        db: Database,
        trade_id: int,
        *,
        leg: int,
        sell_qty_frac: float,
        current_price: float,
        slippage_bps: int = 0,
    ) -> bool:
        """Sell a fraction of original quantity for a ladder leg fill.

        Updates remaining_qty, sets leg_N_filled_at/leg_N_exit_price, increments
        realized_pnl_usd, and (on leg 1) arms the floor. Returns True on success.

        Idempotent: re-calling for the same leg is a no-op when leg_N_filled_at
        is already set (guard against concurrent evaluator ticks).
        """
        if leg not in (1, 2):
            raise ValueError(f"leg must be 1 or 2, got {leg}")
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        cur = await conn.execute(
            f"SELECT entry_price, quantity, remaining_qty, realized_pnl_usd, "
            f"leg_{leg}_filled_at FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        row = await cur.fetchone()
        if row is None:
            log.warning("partial_sell_trade_not_found", trade_id=trade_id, leg=leg)
            return False
        entry_price, initial_qty, remaining_qty, realized, already_filled = row
        if already_filled is not None:
            log.info("partial_sell_already_filled", trade_id=trade_id, leg=leg)
            return False

        effective_exit = current_price * (1 - slippage_bps / 10000)
        if effective_exit <= 0:
            log.warning("partial_sell_zero_price", trade_id=trade_id, leg=leg)
            return False

        leg_qty = float(initial_qty) * sell_qty_frac
        proceeds = leg_qty * effective_exit
        cost = leg_qty * float(entry_price)
        leg_realized = proceeds - cost
        new_remaining = float(remaining_qty) - leg_qty
        new_realized = float(realized) + leg_realized
        now = datetime.now(timezone.utc).isoformat()

        updates = (
            f"UPDATE paper_trades SET remaining_qty = ?, realized_pnl_usd = ?, "
            f"leg_{leg}_filled_at = ?, leg_{leg}_exit_price = ?"
        )
        params: list = [new_remaining, new_realized, now, effective_exit]
        if leg == 1:
            updates += ", floor_armed = 1"
        updates += f" WHERE id = ? AND leg_{leg}_filled_at IS NULL"
        params.append(trade_id)

        await conn.execute(updates, params)
        await conn.commit()

        log.info(
            "ladder_leg_fired",
            trade_id=trade_id, leg=leg, fill_price=effective_exit,
            leg_qty=leg_qty, leg_realized_usd=leg_realized,
            remaining_qty=new_remaining, realized_pnl_usd=new_realized,
        )
        if leg == 1:
            log.info("floor_activated", trade_id=trade_id)
        return True

    async def execute_sell(
        self,
        db: Database,
        trade_id: int,
        current_price: float,
        reason: str,
        slippage_bps: int = 0,
    ) -> bool:
        """Close a paper trade. Applies exit slippage. Returns True if closed.

        effective_exit = price * (1 - bps/10000).
        """
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        cursor = await conn.execute(
            "SELECT entry_price, amount_usd, quantity FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            log.warning("paper_trade_not_found", trade_id=trade_id)
            return False

        entry_price = float(row[0])
        amount_usd = float(row[1])
        quantity = float(row[2])

        effective_exit = current_price * (1 - slippage_bps / 10000)
        if entry_price <= 0:
            log.warning("paper_trade_zero_entry_price", trade_id=trade_id)
            pnl_pct = 0.0
            pnl_usd = 0.0
        else:
            pnl_pct = ((effective_exit - entry_price) / entry_price) * 100
            pnl_usd = quantity * (effective_exit - entry_price)
        now = datetime.now(timezone.utc).isoformat()

        if reason == "take_profit" and pnl_usd < 0:
            log.warning(
                "tp_negative_pnl",
                trade_id=trade_id,
                pnl_usd=round(pnl_usd, 2),
                pnl_pct=round(pnl_pct, 2),
                slippage_bps=slippage_bps,
            )

        status_map = {
            "take_profit": "closed_tp",
            "stop_loss": "closed_sl",
            "expired": "closed_expired",
            "trailing_stop": "closed_trailing_stop",
            "manual": "closed_manual",
        }
        status = status_map.get(reason, "closed_manual")

        cursor_upd = await conn.execute(
            """UPDATE paper_trades
               SET status = ?, exit_price = ?, exit_reason = ?,
                   pnl_usd = ?, pnl_pct = ?, closed_at = ?
               WHERE id = ? AND status = 'open'""",
            (status, effective_exit, reason, pnl_usd, round(pnl_pct, 4), now, trade_id),
        )
        if cursor_upd.rowcount == 0:
            log.warning("trade_already_closed", trade_id=trade_id)
            return False
        await conn.commit()

        log.info(
            "paper_trade_closed",
            trade_id=trade_id,
            reason=reason,
            exit_price=effective_exit,
            pnl_usd=round(pnl_usd, 2),
            pnl_pct=round(pnl_pct, 2),
        )
        return True
