"""TradingEngine -- pluggable interface for paper and live trading."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

import structlog

from scout.db import Database
from scout.trading.paper import PaperTrader
from scout.trading.params import (
    DEFAULT_SIGNAL_TYPES,
    UnknownSignalType,
    get_params,
)

log = structlog.get_logger()

# Maximum age (seconds) for a price_cache entry to be considered fresh.
# Paper trading uses a generous window since signals fire infrequently
# and laggard tokens may only be cached once per narrative cycle (30 min+).
_MAX_PRICE_AGE_SECONDS = 3600  # 1 hour for paper; tighten for live


async def _compute_lead_time_vs_trending(
    db: Database, token_id: str, now: datetime
) -> tuple[float | None, str]:
    """Returns (lead_time_min, status). status in {'ok', 'no_reference', 'error'}.

    Negative lead_time means we opened BEFORE the coin trended (beat CG).
    Positive means we opened AFTER (we were late).
    """
    import aiosqlite

    try:
        cursor = await db._conn.execute(
            "SELECT MIN(snapshot_at) FROM trending_snapshots WHERE coin_id = ?",
            (token_id,),
        )
        row = await cursor.fetchone()
        crossed_at = row[0] if row else None
        if crossed_at is None:
            return (None, "no_reference")
        crossed_dt = datetime.fromisoformat(crossed_at)
        if crossed_dt.tzinfo is None:
            crossed_dt = crossed_dt.replace(tzinfo=timezone.utc)
        delta_min = (now - crossed_dt).total_seconds() / 60.0
        return (delta_min, "ok")
    except (aiosqlite.Error, ValueError, TypeError) as e:
        # Narrow the catch to expected DB / parse errors. Programming bugs
        # (AttributeError, NameError, KeyError) must still crash loudly so
        # they're caught in test instead of permanently degrading the column.
        log.error(
            "lead_time_compute_error",
            err=str(e),
            err_type=type(e).__name__,
            err_id="LEAD_TIME_CALC",
            token_id=token_id,
        )
        return (None, "error")


class TradingEngine:
    """Pluggable trading engine. Call from any signal source.

    Usage:
        engine = TradingEngine(mode="paper", db=db, settings=settings)
        trade_id = await engine.open_trade(
            token_id="bitcoin", chain="coingecko",
            signal_type="volume_spike",
            signal_data={"spike_ratio": 12.3},
        )

    signal_data schema per signal_type:
        volume_spike:         {"spike_ratio": float, "current_price": float}
        narrative_prediction: {"fit": int, "category": str}
        trending_catch:       {"source": str}
        gainers_early:        {"price_change_24h": float}
        losers_contrarian:    {"price_change_24h": float}
        momentum_7d:          {"change_7d": float, "change_24h": float}
        chain_completed:      {"pattern": str, "boost": int}
        first_signal:         {"quant_score": int, "signals": list[str]}
    """

    def __init__(
        self,
        mode: str,
        db: Database,
        settings,
        *,
        live_engine=None,
        session=None,
    ) -> None:
        self.mode = mode
        self.db = db
        self.settings = settings
        # BL-055: when a LiveEngine is wired in (shadow mode), PaperTrader
        # fires a fire-and-forget handoff per trade open. See scout/main.py.
        self._paper_trader = PaperTrader(live_engine=live_engine)
        # Monotonic start marker for the warmup window
        self._started_at = time.monotonic()
        # BL-NEW-TG-ALERT-ALLOWLIST (R1-I3+I5 design fold): aiohttp.ClientSession
        # for TG dispatch. Wired AFTER construction by main.py via
        # set_tg_session(session) inside the cycle's ClientSession block,
        # because the engine is constructed BEFORE the ClientSession
        # context manager opens. None during the brief construction-to-
        # set_tg_session window means TG alerts can't fire — but no paper
        # trades are processed in that window either.
        self._tg_session = session
        # R1-C3 design fold: ref-holding set so spawned alert tasks aren't
        # GC'd mid-flight (mirrors scout/main.py:91 `_social_restart_tasks`
        # pattern). Mid-flight loss on shutdown is acceptable — paper
        # trade row is committed; only the alert + log row is lost.
        self._tg_alert_tasks: set[asyncio.Task] = set()

    def set_tg_session(self, session) -> None:
        """Wire the aiohttp.ClientSession used for TG alert dispatch.

        scout/main.py calls this inside the cycle's `async with
        aiohttp.ClientSession() as session:` block.
        """
        self._tg_session = session

    async def open_trade(
        self,
        token_id: str,
        symbol: str = "",
        name: str = "",
        chain: str = "coingecko",
        signal_type: str = "",
        signal_data: dict | None = None,
        amount_usd: float | None = None,
        entry_price: float | None = None,
        *,
        signal_combo: str,
        expected_empty_metadata: bool = False,
    ) -> int | None:
        """Open a new trade. Returns trade_id or None if rejected.

        If *entry_price* is provided (and > 0), it is used directly instead of
        looking up price_cache.  This is essential for trending/gainers/losers
        signals where the snapshot already contains a fresh price.
        """
        if signal_data is None:
            signal_data = {}

        # BL-076: defense-in-depth visibility for empty symbol+name.
        # Bug 2 (operator audit 2026-05-04) showed ~150+ paper trades had
        # empty symbol+name because 3 dispatchers (volume_spike,
        # narrative_prediction, chain_completed) didn't pass them. The
        # symbol_name population is fixed in scout/trading/signals.py;
        # this guard surfaces any FUTURE caller drift. Placement BEFORE
        # warmup gate is load-bearing — warmup short-circuits return None
        # so a placement after gate would silently swallow the warning
        # during the warmup window.
        #
        # MF-2 fix (PR #67 silent-failure-hunter): chain_completed orphan
        # path KNOWS it has no metadata (Database.lookup_symbol_name_by_coin_id
        # returned ('', '') and dispatcher already logged
        # chain_completed_no_metadata). Without `expected_empty_metadata=True`
        # opt-in, every orphan chain trade triple-fires events
        # (dispatcher WARNING + this WARNING + this INFO), making
        # Self-Review #8's "14d zero trade_metadata_empty events" soak
        # criterion structurally unreachable. The sentinel preserves
        # caller-drift detection (default False) while letting known-empty
        # callers bypass the engine signal.
        #
        # Two events emitted (only when expected_empty_metadata=False):
        # - WARNING: human-readable journalctl visibility
        # - INFO trade_metadata_empty: lands in same telemetry pipeline
        #   that aggregates signal_skipped_* events. Future BL-077 (after
        #   14d clean soak) flips warning+proceed to log+return-None
        #   using SAME event name (trade_skipped_empty_metadata) — purely
        #   additive change at that point.
        if not symbol and not name and not expected_empty_metadata:
            log.warning(
                "open_trade_called_with_empty_symbol_and_name",
                token_id=token_id,
                signal_type=signal_type,
                signal_combo=signal_combo,
                hint="dispatcher likely missing symbol=... + name=... kwargs",
            )
            log.info(
                "trade_metadata_empty",
                reason="empty_metadata",
                token_id=token_id,
                signal_type=signal_type,
                signal_combo=signal_combo,
            )

        conn = self.db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        # 0a. Startup warmup — coarsest gate (no DB, no allocations).
        # Runs before signal_params lookup so we don't hit the DB for
        # every rejected-by-warmup call in the first N seconds after boot.
        warmup = getattr(self.settings, "PAPER_STARTUP_WARMUP_SECONDS", 0) or 0
        if warmup > 0:
            elapsed = time.monotonic() - self._started_at
            if elapsed < warmup:
                log.info(
                    "trade_skipped_warmup",
                    token_id=token_id,
                    signal_type=signal_type,
                    elapsed=round(elapsed, 1),
                    warmup=warmup,
                )
                return None

        # 0b. Tier 1a kill switch + per-signal params lookup. Comes after
        # the no-DB warmup so the warmup-skip path is allocation-free, but
        # before the price/duplicate/exposure DB hits so a disabled signal
        # short-circuits before paying for those reads. UnknownSignalType
        # is a caller bug (typo) — fail loud, not silently inherit globals.
        try:
            signal_params = await get_params(self.db, signal_type, self.settings)
        except UnknownSignalType:
            log.error(
                "trade_skipped_unknown_signal_type",
                err_id="SIGNAL_PARAMS_UNKNOWN_TYPE",
                token_id=token_id,
                signal_type=signal_type,
                known=sorted(DEFAULT_SIGNAL_TYPES),
            )
            return None
        if not signal_params.enabled:
            log.info(
                "trade_skipped_signal_disabled",
                token_id=token_id,
                signal_type=signal_type,
                source=signal_params.source,
            )
            return None

        # 1. Resolve current price -- prefer caller-supplied entry_price
        if entry_price is not None and entry_price > 0:
            current_price = entry_price
        else:
            price_row = await self._get_current_price_with_age(token_id)
            if price_row is None:
                log.info("trade_skipped_no_price", token_id=token_id)
                return None

            current_price, price_age_seconds = price_row
            if price_age_seconds > _MAX_PRICE_AGE_SECONDS:
                log.info(
                    "trade_skipped_stale_price",
                    token_id=token_id,
                    price_age_seconds=round(price_age_seconds, 1),
                )
                return None

        # Note: TOCTOU gap between duplicate/exposure check and insert is mitigated
        # by asyncio's single-threaded event loop — only one coroutine runs at a time.
        # For true concurrency (multi-process), wrap in BEGIN IMMEDIATE.

        # 2a. Block if there's an open trade on this token (any signal_type) —
        # prevents doubled exposure on the same asset.
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE token_id = ? AND status = 'open'",
            (token_id,),
        )
        row = await cursor.fetchone()
        if row[0] > 0:
            log.info(
                "trade_skipped_open_position",
                token_id=token_id,
                signal_type=signal_type,
            )
            return None

        # 2b. Per-signal-type cooldown — block re-entry within 48h for the
        # same (token, signal_type) pair. Different signal types (e.g.
        # narrative_prediction on a token that had a first_signal yesterday)
        # are allowed through to diversify signal coverage.
        cursor = await conn.execute(
            """SELECT COUNT(*) FROM paper_trades
               WHERE token_id = ? AND signal_type = ?
                 AND datetime(opened_at) >= datetime('now', '-48 hours')""",
            (token_id, signal_type),
        )
        row = await cursor.fetchone()
        if row[0] > 0:
            log.info(
                "trade_skipped_cooldown", token_id=token_id, signal_type=signal_type
            )
            return None

        # 2c. Hard cap on concurrent open positions — prevents restart-bursts.
        max_open = getattr(self.settings, "PAPER_MAX_OPEN_TRADES", 0) or 0
        if max_open > 0:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE status = 'open'"
            )
            row = await cursor.fetchone()
            if row[0] >= max_open:
                log.info(
                    "trade_skipped_max_open_trades",
                    token_id=token_id,
                    signal_type=signal_type,
                    open_count=row[0],
                    max_open=max_open,
                )
                return None

        # 3. Check max exposure
        trade_amount = amount_usd or self.settings.PAPER_TRADE_AMOUNT_USD
        cursor = await conn.execute(
            "SELECT COALESCE(SUM(amount_usd), 0) FROM paper_trades WHERE status = 'open'"
        )
        row = await cursor.fetchone()
        current_exposure = float(row[0])
        if current_exposure + trade_amount > self.settings.PAPER_MAX_EXPOSURE_USD:
            log.warning(
                "trade_rejected_max_exposure",
                token_id=token_id,
                current_exposure=current_exposure,
                new_amount=trade_amount,
                max_exposure=self.settings.PAPER_MAX_EXPOSURE_USD,
            )
            return None

        # 4. Compute lead-time vs trending before executing
        now_utc = datetime.now(timezone.utc)
        lead_time_min, lead_time_status = await _compute_lead_time_vs_trending(
            self.db, token_id, now_utc
        )

        # 5. Execute via paper trader
        if self.mode == "paper":
            trade_id = await self._paper_trader.execute_buy(
                db=self.db,
                token_id=token_id,
                symbol=symbol,
                name=name,
                chain=chain,
                signal_type=signal_type,
                signal_data=signal_data,
                current_price=current_price,
                amount_usd=trade_amount,
                tp_pct=self.settings.PAPER_TP_PCT,
                # Per-signal sl_pct stamps onto the row so the evaluator
                # respects the params in effect at open time, even if
                # calibration changes them later.
                sl_pct=signal_params.sl_pct,
                slippage_bps=self.settings.PAPER_SLIPPAGE_BPS,
                signal_combo=signal_combo,
                lead_time_vs_trending_min=lead_time_min,
                lead_time_vs_trending_status=lead_time_status,
            )
            # BL-NEW-TG-ALERT-ALLOWLIST: post-open Telegram alert hook.
            # Fire-and-forget; never blocks paper-trade success path.
            if trade_id is not None:
                await self._spawn_tg_alert(
                    trade_id=trade_id,
                    signal_type=signal_type,
                    token_id=token_id,
                    symbol=symbol,
                    amount_usd=trade_amount,
                    signal_data=signal_data,
                )
            return trade_id

        log.warning("trade_mode_not_supported", mode=self.mode)
        return None

    async def _spawn_tg_alert(
        self,
        *,
        trade_id: int,
        signal_type: str,
        token_id: str,
        symbol: str,
        amount_usd: float,
        signal_data: dict,
    ) -> None:
        """Spawn the TG alert dispatch as a tracked background task.

        R1-C2 design fold: re-read entry_price from paper_trades AFTER
        execute_buy so the alert's price matches the audit row (post-slip).

        R1-C3 design fold: hold task reference in self._tg_alert_tasks +
        register done_callback to drain on completion. Mirrors
        scout/main.py:91 `_social_restart_tasks` GC-protect pattern.
        Mid-flight loss on engine shutdown is acceptable — paper_trades
        row is already committed; only TG alert + tg_alert_log row is lost.
        """
        from scout.trading.tg_alert_dispatch import notify_paper_trade_opened

        # Post-slip entry price (R1-C2)
        try:
            cur = await self.db._conn.execute(
                "SELECT entry_price FROM paper_trades WHERE id = ?",
                (trade_id,),
            )
            row = await cur.fetchone()
            effective_entry = float(row[0]) if row and row[0] is not None else 0.0
        except Exception:
            log.exception("tg_alert_post_open_price_read_failed", trade_id=trade_id)
            effective_entry = 0.0

        task = asyncio.create_task(
            notify_paper_trade_opened(
                self.db,
                self.settings,
                self._tg_session,
                paper_trade_id=trade_id,
                signal_type=signal_type,
                token_id=token_id,
                symbol=symbol,
                entry_price=effective_entry,
                amount_usd=amount_usd,
                signal_data=signal_data,
            )
        )
        self._tg_alert_tasks.add(task)
        task.add_done_callback(self._tg_alert_tasks.discard)

    async def close_trade(self, trade_id: int, reason: str = "manual") -> None:
        """Force-close a trade."""
        conn = self.db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

        # Get current price for PnL calculation
        cursor = await conn.execute(
            "SELECT token_id FROM paper_trades WHERE id = ?", (trade_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return

        token_id = row[0]
        price_row = await self._get_current_price_with_age(token_id)
        if price_row is None:
            log.warning("close_trade_no_price", trade_id=trade_id, token_id=token_id)
            return
        current_price, age = price_row
        if age > 3600:
            log.warning("close_trade_stale_price", trade_id=trade_id, age=round(age, 1))
            return  # don't close at stale price

        await self._paper_trader.execute_sell(
            db=self.db,
            trade_id=trade_id,
            current_price=current_price,
            reason=reason,
            slippage_bps=self.settings.PAPER_SLIPPAGE_BPS,
        )

    async def get_open_positions(self) -> list[dict]:
        """All open paper trades."""
        conn = self.db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await conn.execute(
            "SELECT * FROM paper_trades WHERE status = 'open' ORDER BY opened_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def get_pnl_summary(self, days: int = 7) -> dict:
        """Aggregate PnL statistics over the last N days.

        Excludes long_hold trades from main stats to avoid inflating PnL
        with positions that have different risk/reward profiles.
        """
        conn = self.db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await conn.execute(
            """SELECT
                 COUNT(*) as total_trades,
                 SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins,
                 SUM(CASE WHEN pnl_usd < 0 THEN 1 ELSE 0 END) as losses,
                 COALESCE(SUM(pnl_usd), 0) as total_pnl_usd,
                 COALESCE(AVG(pnl_pct), 0) as avg_pnl_pct,
                 MAX(pnl_usd) as best_trade,
                 MIN(pnl_usd) as worst_trade
               FROM paper_trades
               WHERE status != 'open'
                 AND signal_type != 'long_hold'
                 AND datetime(closed_at) >= datetime('now', ?)""",
            (f"-{days} days",),
        )
        row = await cursor.fetchone()
        total = row[0] or 0
        wins = row[1] or 0
        return {
            "total_trades": total,
            "wins": wins,
            "losses": row[2] or 0,
            "total_pnl_usd": row[3] or 0,
            "avg_pnl_pct": round(row[4] or 0, 2),
            "best_trade": row[5],
            "worst_trade": row[6],
            "win_rate_pct": round((wins / total) * 100, 1) if total > 0 else 0,
        }

    async def get_pnl_by_signal_type(self, days: int = 7) -> dict:
        """PnL breakdown by signal type.

        long_hold trades are reported in a separate key so they don't
        inflate the main signal-type stats.
        """
        conn = self.db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")
        cursor = await conn.execute(
            """SELECT signal_type,
                 COUNT(*) as trades,
                 COALESCE(SUM(pnl_usd), 0) as pnl,
                 SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins
               FROM paper_trades
               WHERE status != 'open'
                 AND signal_type != 'long_hold'
                 AND datetime(closed_at) >= datetime('now', ?)
               GROUP BY signal_type""",
            (f"-{days} days",),
        )
        rows = await cursor.fetchall()
        result = {}
        for row in rows:
            total = row[1]
            wins = row[3] or 0
            result[row[0]] = {
                "trades": total,
                "pnl": round(row[2], 2),
                "win_rate": round((wins / total) * 100, 1) if total > 0 else 0,
            }

        # Report long_hold separately so callers can display it distinctly
        lh_cursor = await conn.execute(
            """SELECT COUNT(*) as trades,
                 COALESCE(SUM(pnl_usd), 0) as pnl,
                 SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) as wins
               FROM paper_trades
               WHERE status != 'open'
                 AND signal_type = 'long_hold'
                 AND datetime(closed_at) >= datetime('now', ?)""",
            (f"-{days} days",),
        )
        lh_row = await lh_cursor.fetchone()
        lh_total = lh_row[0] or 0
        if lh_total > 0:
            lh_wins = lh_row[2] or 0
            result["long_hold"] = {
                "trades": lh_total,
                "pnl": round(lh_row[1], 2),
                "win_rate": round((lh_wins / lh_total) * 100, 1) if lh_total > 0 else 0,
                "separate": True,
            }
        return result

    async def _get_current_price_with_age(
        self, token_id: str
    ) -> tuple[float, float] | None:
        """Look up price from price_cache table. Returns (price, age_seconds) or None."""
        conn = self.db._conn
        if conn is None:
            return None
        cursor = await conn.execute(
            "SELECT current_price, updated_at FROM price_cache WHERE coin_id = ?",
            (token_id,),
        )
        row = await cursor.fetchone()
        if row is not None and row[0] is not None:
            price = float(row[0])
            updated_at = datetime.fromisoformat(str(row[1])).replace(
                tzinfo=timezone.utc
            )
            age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
            return (price, age_seconds)

        # M2: Fuzzy fallback removed -- it matched wrong assets more often
        # than it helped.  If the exact coin_id is not cached, return None
        # and let the caller skip the trade.
        return None
