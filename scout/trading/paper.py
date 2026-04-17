"""PaperTrader -- simulates trade execution by logging to DB at current price."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import structlog

from scout.db import Database

log = structlog.get_logger()


class PaperTrader:
    """Simulates trade execution with slippage simulation."""

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
    ) -> int | None:
        """Record a paper buy. Returns trade ID or None if rejected.

        Applies slippage to entry price: effective_entry = price * (1 + bps/10000).
        sl_pct is positive: sl_price = entry * (1 - sl_pct/100).
        """
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        effective_entry = current_price * (1 + slippage_bps / 10000)
        if effective_entry <= 0:
            log.warning("paper_trade_zero_price", token_id=token_id, current_price=current_price)
            return None
        quantity = amount_usd / effective_entry
        # Sanity check: quantity must be positive and finite
        if quantity <= 0 or not (quantity == quantity):  # NaN check
            log.warning("paper_trade_invalid_quantity", token_id=token_id, quantity=quantity)
            return None
        tp_price = effective_entry * (1 + tp_pct / 100)
        sl_price = effective_entry * (1 - sl_pct / 100) if sl_pct > 0 else 0.0
        now = datetime.now(timezone.utc).isoformat()

        cursor = await conn.execute(
            """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity,
                tp_pct, sl_pct, tp_price, sl_price,
                status, opened_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (
                token_id, symbol, name, chain, signal_type,
                json.dumps(signal_data),
                effective_entry, amount_usd, quantity,
                tp_pct, sl_pct, tp_price, sl_price,
                now,
            ),
        )
        await conn.commit()
        trade_id = cursor.lastrowid

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
        return trade_id

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

        # Map reason to status
        status_map = {
            "take_profit": "closed_tp",
            "stop_loss": "closed_sl",
            "expired": "closed_expired",
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
