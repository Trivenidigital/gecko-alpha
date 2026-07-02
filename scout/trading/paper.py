"""PaperTrader -- simulates trade execution by logging to DB at current price."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog
from pydantic import ValidationError

from scout.config import Settings
from scout.db import Database
from scout.exceptions import MoonshotArmFailed
from scout.price_sources import (
    EXIT_PROVENANCE_MARKET,
    EXIT_PROVENANCES,
    resolve_price_source,
)
from scout.trading.models import PaperTradeOpen
from scout.trading.actionability import (
    ActionabilityDecision,
    evaluate_actionability_v1,
)
from scout.trading.conviction import compute_stack
from scout.trading.live_eligibility import compute_would_be_live

if TYPE_CHECKING:
    from scout.live.engine import LiveEngine

log = structlog.get_logger()


def _log_live_handoff_task_exception(task: asyncio.Task) -> None:
    """GA-11 done-callback: surface fire-and-forget live-handoff failures.

    Retrieves ``task.exception()`` and logs it — the prior discard-only
    callback made a crashing ``LiveEngine.on_paper_trade_opened`` handoff
    completely invisible (never awaited, never logged).
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("live_handoff_task_failed", error=str(exc), exc_info=exc)


CLOSED_COUNTABLE_STATUSES: tuple[str, ...] = (
    "closed_tp",
    "closed_sl",
    "closed_expired",
    "closed_trailing_stop",
    "closed_moonshot_trail",
    # Phase 6 slice 3: stale-onset exits carry a real-ish mark (last-good
    # cached price) — excluded from NOTHING by default. Consumers that want
    # to discount them can key on exit_provenance='stale_snapshot'.
    "closed_stale_onset",
)


@dataclass
class _PaperTradeHandoff:
    """Minimal handoff payload passed to LiveEngine.on_paper_trade_opened."""

    id: int
    signal_type: str
    symbol: str
    coin_id: str


def _has_usable_mcap(signal_data: dict) -> bool:
    for key in (
        "mcap",
        "market_cap",
        "market_cap_usd",
        "mcap_at_sighting",
        "alert_market_cap",
    ):
        value = signal_data.get(key)
        if value in (None, ""):
            continue
        try:
            float(value)
            return True
        except (TypeError, ValueError):
            continue
    return False


async def _enrich_actionability_signal_data(
    db: Database,
    *,
    token_id: str,
    signal_type: str,
    signal_data: dict,
) -> dict:
    enriched = dict(signal_data)
    if _has_usable_mcap(enriched):
        return enriched

    conn = db._conn
    if conn is None:
        return enriched

    try:
        if signal_type == "chain_completed":
            cur = await conn.execute(
                "SELECT mcap_at_completion FROM chain_matches "
                "WHERE token_id=? AND mcap_at_completion IS NOT NULL "
                "ORDER BY datetime(completed_at) DESC LIMIT 1",
                (token_id,),
            )
            row = await cur.fetchone()
            if row and row[0] not in (None, ""):
                enriched["mcap"] = row[0]
                return enriched

        cur = await conn.execute(
            "SELECT market_cap FROM price_cache WHERE coin_id=?",
            (token_id,),
        )
        row = await cur.fetchone()
        if row and row[0] not in (None, ""):
            enriched["mcap"] = row[0]
    except Exception:
        log.exception(
            "actionability_mcap_enrichment_failed",
            token_id=token_id,
            signal_type=signal_type,
        )
    return enriched


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
        price_source: str | None = None,
    ) -> int | None:
        """Record a paper buy. Returns trade ID, or None if rejected by guards.

        Applies slippage to entry price: effective_entry = price * (1 + bps/10000).
        sl_pct is positive: sl_price = entry * (1 - sl_pct/100).

        *price_source* (Phase 6 slice 2): the registered price source that
        will re-price this position after open. Callers that already
        resolved it (TradingEngine gate 0c) pass it through; when None it
        is resolved here from the registry. Either way the
        :class:`~scout.trading.models.PaperTradeOpen` boundary model
        REFUSES to open without a registered value — this is the hard
        invariant, independent of the engine's (flag-gated) dispatch gate.
        """
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        # Phase 6 slice 2 — open-boundary invariant. Resolve when the
        # caller didn't, then validate via the app-boundary model.
        if price_source is None:
            cur = await conn.execute(
                "SELECT 1 FROM price_cache WHERE coin_id = ? LIMIT 1",
                (token_id,),
            )
            price_source = resolve_price_source(
                token_id, (await cur.fetchone()) is not None
            )
        try:
            PaperTradeOpen(
                token_id=token_id,
                signal_type=signal_type,
                signal_combo=signal_combo,
                price_source=price_source,
            )
        except ValidationError as exc:
            log.warning(
                "paper_trade_rejected_unregistered_price_source",
                token_id=token_id,
                signal_type=signal_type,
                signal_combo=signal_combo,
                price_source=price_source,
                err=str(exc),
                hint=(
                    "no registered price source can re-price this token_id; "
                    "opening would recreate the GA-01 unpriceable-position class"
                ),
            )
            return None

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
        actionability: ActionabilityDecision | None = None
        stack_for_actionability = 0
        try:
            if signal_type not in ("chain_completed", "volume_spike"):
                stack_for_actionability = await compute_stack(db, token_id, now)
        except Exception:
            log.exception(
                "actionability_stack_compute_failed",
                token_id=token_id,
                signal_type=signal_type,
            )
            if signal_type == "gainers_early":
                actionability = ActionabilityDecision(
                    False, "v1_block_gainers_early_stack_unavailable", "v1"
                )
            stack_for_actionability = 0

        # BL-NEW-ACTIONABILITY-ENTRY-SNAPSHOT-FOUNDATION (Vector B I-B2 fold):
        # default the snapshot-input signal_data to the raw signal_data; if
        # enrichment runs successfully, switch to the enriched dict so the
        # snapshot captures the SAME mcap the classifier saw.
        snapshot_signal_data = signal_data

        if actionability is None:
            try:
                actionability_signal_data = await _enrich_actionability_signal_data(
                    db,
                    token_id=token_id,
                    signal_type=signal_type,
                    signal_data=signal_data,
                )
                snapshot_signal_data = actionability_signal_data
                actionability = evaluate_actionability_v1(
                    signal_type=signal_type,
                    signal_data=actionability_signal_data,
                    signal_combo=signal_combo,
                    conviction_stack=stack_for_actionability,
                )
            except Exception:
                log.exception(
                    "actionability_gate_failed",
                    token_id=token_id,
                    signal_type=signal_type,
                )
                actionability = ActionabilityDecision(False, "v1_error", "v1")

        actionable_value = 1 if actionability.actionable else 0
        actionability_reason = actionability.reason
        actionability_version = actionability.version

        would_be_live = 0
        if settings is not None:
            try:
                would_be_live = await compute_would_be_live(
                    db,
                    signal_type=signal_type,
                    signal_data=signal_data,
                    conviction_stack=stack_for_actionability,
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
   remaining_qty, floor_armed, realized_pnl_usd, would_be_live,
   actionable, actionability_reason, actionability_version, price_source)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?,
        ?, 0, 0.0, ?, ?, ?, ?, ?)
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
                actionable_value,
                actionability_reason,
                actionability_version,
                price_source,
            ),
        )
        trade_id = cursor.lastrowid
        await conn.commit()

        # BL-NEW-ACTIONABILITY-ENTRY-SNAPSHOT-FOUNDATION: metadata-only stamp
        # of point-in-time entry facts. Wrapped so any failure degrades to a
        # structured log and never blocks the trade-open contract (the trade
        # is already committed at this point).
        try:
            from scout.trading.entry_snapshot import stamp_entry_snapshot

            await stamp_entry_snapshot(
                db,
                trade_id=trade_id,
                opened_at=now,
                signal_type=signal_type,
                # Vector B I-B2 fold: pass enriched signal_data so the
                # snapshot captures the SAME mcap the classifier saw.
                # Falls back to raw signal_data on enrichment failure.
                signal_data=snapshot_signal_data,
                signal_combo=signal_combo,
                tp_pct=tp_pct,
                sl_pct=sl_pct,
                actionable_value=actionable_value,
                actionability_reason=actionability_reason,
                actionability_version=actionability_version,
                contract_address=token_id,
                chain=chain,
                settings=settings,
            )
        except Exception:
            log.exception(
                "entry_snapshot_stamp_failed",
                trade_id=trade_id,
                token_id=token_id,
                signal_type=signal_type,
            )

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
            actionable=actionable_value,
            actionability_reason=actionability_reason,
            actionability_version=actionability_version,
            price_source=price_source,
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
            # GA-11: retrieve + log the exception; a discard-only callback
            # left live-handoff failures completely invisible.
            task.add_done_callback(_log_live_handoff_task_exception)

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
        price_provenance: str = EXIT_PROVENANCE_MARKET,
    ) -> bool:
        """Sell a fraction of original quantity for a ladder leg fill.

        Updates remaining_qty, sets leg_N_filled_at/leg_N_exit_price, increments
        realized_pnl_usd, and (on leg 1) arms the floor. Returns True on success.

        Idempotent: re-calling for the same leg is a no-op when leg_N_filled_at
        is already set (guard against concurrent evaluator ticks).

        *price_provenance* (Phase 6 slice 2): where *current_price* came
        from — one of :data:`scout.price_sources.EXIT_PROVENANCES`. Leg
        fills only fire on fresh prices today, so 'market' is the only
        value the evaluator passes; the kwarg exists so no future caller
        can record a fill without saying what its price was. Logged on
        the ``ladder_leg_fired`` event (the trade stays open — the
        ``exit_provenance`` column is stamped by the final close).
        """
        if leg not in (1, 2):
            raise ValueError(f"leg must be 1 or 2, got {leg}")
        if price_provenance not in EXIT_PROVENANCES:
            raise ValueError(
                f"price_provenance {price_provenance!r} is not registered "
                f"(registered: {sorted(EXIT_PROVENANCES)})"
            )
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
            log.warning("partial_sell_race_lost", trade_id=trade_id, leg=leg)
            return False
        await conn.commit()

        peak_pct_rounded = round(float(peak_pct), 2) if peak_pct is not None else None
        log.info(
            "ladder_leg_fired",
            trade_id=trade_id,
            leg=leg,
            price_provenance=price_provenance,
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
        price_provenance: str = EXIT_PROVENANCE_MARKET,
    ) -> bool:
        """Close a paper trade. Applies exit slippage. Returns True if closed.

        effective_exit = price * (1 - bps/10000).

        *price_provenance* (Phase 6 slice 2): where *current_price* came
        from — one of :data:`scout.price_sources.EXIT_PROVENANCES`
        ('market' | 'stale_snapshot' | 'entry_fallback'). Persisted to
        ``paper_trades.exit_provenance`` so a close can never be recorded
        without saying what its price was. Defaults to 'market' (the
        normal fresh-price exit); the evaluator's forced-close paths pass
        their true provenance explicitly.
        """
        if price_provenance not in EXIT_PROVENANCES:
            raise ValueError(
                f"price_provenance {price_provenance!r} is not registered "
                f"(registered: {sorted(EXIT_PROVENANCES)})"
            )
        conn = db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        cursor = await conn.execute(
            "SELECT entry_price, amount_usd, quantity, remaining_qty, "
            "realized_pnl_usd FROM paper_trades WHERE id = ?",
            (trade_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            log.warning("paper_trade_not_found", trade_id=trade_id)
            return False

        entry_price = float(row[0])
        amount_usd = float(row[1])
        quantity = float(row[2])
        # Two partial mechanisms shrink DIFFERENT columns: the BL-061 ladder
        # (execute_partial_sell) banks leg gains into realized_pnl_usd and
        # decrements remaining_qty (quantity stays original); the legacy
        # pre-cutover partial-TP (evaluator.py) shrinks quantity (remaining_qty
        # stays original). Close the RUNNER on the actually-held qty =
        # min(remaining_qty, quantity) — correct for BOTH — and FOLD IN the banked
        # realized_pnl_usd. Otherwise laddered winners are understated (runner-only
        # PnL on the full original quantity) and realized_pnl_usd is silently dropped
        # from every closed-trade consumer (combo_performance, auto-suspend,
        # calibration, digests, dashboard PnL). Normal full close: remaining_qty ==
        # quantity and realized == 0, so this is a no-op (pre-cutover NULLs coalesced).
        remaining_qty = float(row[3]) if row[3] is not None else quantity
        realized_pnl_usd = float(row[4]) if row[4] is not None else 0.0
        held_qty = min(remaining_qty, quantity)

        effective_exit = current_price * (1 - slippage_bps / 10000)
        if entry_price <= 0:
            log.warning("paper_trade_zero_entry_price", trade_id=trade_id)
            pnl_pct = 0.0
            pnl_usd = 0.0
        else:
            runner_pnl = held_qty * (effective_exit - entry_price)
            pnl_usd = realized_pnl_usd + runner_pnl
            # Blend over the full original notional (quantity*entry). For the legacy
            # partial-TP path quantity is the shrunk sold-portion, so this reduces to
            # the pre-fix (exit-entry)/entry%; for ladder it blends over the original.
            notional = quantity * entry_price
            pnl_pct = (pnl_usd / notional) * 100 if notional > 0 else 0.0
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
                   exit_provenance = ?, pnl_usd = ?, pnl_pct = ?, closed_at = ?
               WHERE id = ? AND status = 'open'""",
            (
                status,
                effective_exit,
                reason,
                price_provenance,
                pnl_usd,
                round(pnl_pct, 4),
                now,
                trade_id,
            ),
        )
        if cursor_upd.rowcount == 0:
            log.warning("trade_already_closed", trade_id=trade_id)
            return False
        await conn.commit()

        log.info(
            "paper_trade_closed",
            trade_id=trade_id,
            reason=reason,
            price_provenance=price_provenance,
            exit_price=effective_exit,
            pnl_usd=round(pnl_usd, 2),
            pnl_pct=round(pnl_pct, 2),
        )
        return True
