"""PaperTrader -- simulates trade execution by logging to DB at current price."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

from scout.config import Settings
from scout.db import Database
from scout.exceptions import MoonshotArmFailed
from scout.trading.conviction import compute_stack
from scout.trading.live_eligibility import compute_would_be_live

if TYPE_CHECKING:
    from scout.live.engine import LiveEngine

log = structlog.get_logger()


CLOSED_COUNTABLE_STATUSES: tuple[str, ...] = (
    "closed_tp",
    "closed_sl",
    "closed_expired",
    "closed_trailing_stop",
    "closed_moonshot_trail",
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
        settings: Settings | None = None,
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

        # BL-NEW-LIVE-ELIGIBLE: stamp would_be_live flag. Defensive — any
        # failure here returns 0 so paper-trade open is never blocked.
        # PR-review NIT fold: skip compute_stack for chain_completed +
        # volume_spike (unconditionally Tier 1a/2a; stack value unused).
        # Other signal_types may pass via Tier 1b (stack ≥ 3) OR Tier 2b
        # (gainers_early thresholds), so stack must be computed.
        would_be_live = 0
        if settings is not None:
            try:
                if signal_type in ("chain_completed", "volume_spike"):
                    stack = 0
                else:
                    stack = await compute_stack(db, token_id, now)
                would_be_live = await compute_would_be_live(
                    db,
                    signal_type=signal_type,
                    signal_data=signal_data,
                    conviction_stack=stack,
                    settings=settings,
                )
            except Exception:
                log.exception(
                    "would_be_live_stamp_failed",
                    token_id=token_id,
                    signal_type=signal_type,
                )
                would_be_live = 0

        INSERT_SQL = """
INSERT INTO paper_trades
  (token_id, symbol, name, chain, signal_type, signal_data,
   entry_price, amount_usd, quantity,
   tp_pct, sl_pct, tp_price, sl_price,
   status, opened_at,
   signal_combo, lead_time_vs_trending_min, lead_time_vs_trending_status,
   remaining_qty, floor_armed, realized_pnl_usd, would_be_live)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?,
        ?, 0, 0.0, ?)
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
                would_be_live,
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
            would_be_live=would_be_live,
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

    async def arm_moonshot(
        self,
        db: Database,
        trade_id: int,
        *,
        peak_pct_at_arm: float,
        original_trail_drawdown_pct: float,
    ) -> str | None:
        """Atomically arm moonshot exit on a trade.

        Single conditional UPDATE WHERE moonshot_armed_at IS NULL with a
        rowcount check — mirrors the proven `execute_partial_sell` pattern
        for race-safety against concurrent evaluator ticks.

        Returns the ISO timestamp written to `moonshot_armed_at` on success
        so callers use the exact DB value rather than re-stamping a
        microsecond-drifted `datetime.now()`. Returns None when the trade
        was already armed (repeat call or race-lost — both look the same
        after serialization through aiosqlite).

        Raises MoonshotArmFailed when the UPDATE matches zero rows AND the
        trade row genuinely doesn't exist (vs the normal already-armed
        case). This distinction prevents silently treating "trade missing"
        as "trade already armed" — a real bug would never surface.

        Race-safety against the verify SELECT relies on paper_trades being
        append-only (BL-055 enforces this via ON DELETE RESTRICT on
        live_trades.paper_trade_id). Without the contract a delete between
        UPDATE and verify could falsely raise MoonshotArmFailed.
        """
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        now = datetime.now(timezone.utc).isoformat()
        cursor = await conn.execute(
            "UPDATE paper_trades "
            "SET moonshot_armed_at = ?, original_trail_drawdown_pct = ? "
            "WHERE id = ? AND moonshot_armed_at IS NULL",
            (now, original_trail_drawdown_pct, trade_id),
        )
        # Always close the implicit transaction the UPDATE may have opened,
        # whether it changed a row or not. Symmetric on rowcount=0 and =1
        # keeps connection-level transaction state predictable for the
        # verify SELECT below and any caller that runs more queries after.
        await conn.commit()

        if cursor.rowcount == 1:
            log.info(
                "moonshot_armed",
                trade_id=trade_id,
                peak_pct_at_arm=round(float(peak_pct_at_arm), 2),
                original_trail_drawdown_pct=original_trail_drawdown_pct,
            )
            return now

        # rowcount == 0: distinguish "already armed" (normal) from "trade
        # doesn't exist" (bug). One extra round-trip on the cold path; the
        # hot path took the rowcount==1 branch above. The verify-SELECT race
        # window is closed by the BL-055 ON DELETE RESTRICT contract on
        # live_trades.paper_trade_id — paper_trades rows cannot be deleted
        # while a live trade references them. If that contract is ever
        # loosened, this branch can falsely raise MoonshotArmFailed.
        verify = await conn.execute(
            "SELECT moonshot_armed_at FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        try:
            row = await verify.fetchone()
        finally:
            await verify.close()
        if row is None:
            raise MoonshotArmFailed(f"arm_moonshot: trade_id={trade_id} not found")
        log.info("moonshot_arm_skipped_already_armed", trade_id=trade_id)
        return None

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
        leg = int(leg)
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        cur = await conn.execute(
            f"SELECT entry_price, quantity, remaining_qty, realized_pnl_usd, "
            f"leg_{leg}_filled_at, peak_pct FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        row = await cur.fetchone()
        if row is None:
            log.warning("partial_sell_trade_not_found", trade_id=trade_id, leg=leg)
            return False
        entry_price, initial_qty, remaining_qty, realized, already_filled, peak_pct = (
            row
        )
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

        cursor_upd = await conn.execute(updates, params)
        if cursor_upd.rowcount == 0:
            await conn.commit()
            log.warning("partial_sell_race_lost", trade_id=trade_id, leg=leg)
            return False
        await conn.commit()

        peak_pct_rounded = round(float(peak_pct), 2) if peak_pct is not None else None
        log.info(
            "ladder_leg_fired",
            trade_id=trade_id,
            leg=leg,
            fill_price=effective_exit,
            leg_qty=leg_qty,
            leg_realized_usd=leg_realized,
            remaining_qty=new_remaining,
            realized_pnl_usd=new_realized,
            peak_pct_at_fire=peak_pct_rounded,
        )
        if leg == 1:
            log.info(
                "floor_activated",
                trade_id=trade_id,
                peak_pct_at_activation=peak_pct_rounded,
            )
        return True

    async def execute_sell(
        self,
        db: Database,
        trade_id: int,
        current_price: float,
        reason: str,
        slippage_bps: int = 0,
        *,
        status_override: str | None = None,
    ) -> bool:
        """Close a paper trade. Applies exit slippage. Returns True if closed.

        effective_exit = price * (1 - bps/10000).
        """
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        cursor = await conn.execute(
            "SELECT entry_price, amount_usd, quantity, remaining_qty, realized_pnl_usd "
            "FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            log.warning("paper_trade_not_found", trade_id=trade_id)
            return False

        entry_price = float(row[0])
        amount_usd = float(row[1])
        quantity = float(row[2])
        remaining_qty = row[3]
        realized_pnl_usd = row[4]
        close_qty = float(remaining_qty) if remaining_qty is not None else quantity
        realized_so_far = (
            float(realized_pnl_usd) if realized_pnl_usd is not None else 0.0
        )

        effective_exit = current_price * (1 - slippage_bps / 10000)
        if entry_price <= 0:
            log.warning("paper_trade_zero_entry_price", trade_id=trade_id)
            pnl_pct = 0.0
            pnl_usd = 0.0
        else:
            final_leg_pnl = close_qty * (effective_exit - entry_price)
            pnl_usd = realized_so_far + final_leg_pnl
            pnl_pct = (pnl_usd / amount_usd) * 100 if amount_usd else 0.0
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
            "peak_fade": "closed_peak_fade",
            "manual": "closed_manual",
        }
        status = (
            status_override
            if status_override is not None
            else status_map.get(reason, "closed_manual")
        )

        cursor_upd = await conn.execute(
            """UPDATE paper_trades
               SET status = ?, exit_price = ?, exit_reason = ?,
                   pnl_usd = ?, pnl_pct = ?, closed_at = ?,
                   remaining_qty = CASE
                       WHEN remaining_qty IS NULL THEN remaining_qty
                       ELSE 0
                   END
               WHERE id = ? AND status = 'open'""",
            (status, effective_exit, reason, pnl_usd, round(pnl_pct, 4), now, trade_id),
        )
        if cursor_upd.rowcount == 0:
            await conn.commit()
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
