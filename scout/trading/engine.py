"""TradingEngine -- pluggable interface for paper and live trading."""

from __future__ import annotations

from datetime import datetime, timezone

import structlog

from scout.db import Database
from scout.trading.paper import PaperTrader

log = structlog.get_logger()

# Maximum age (seconds) for a price_cache entry to be considered fresh.
# Paper trading uses a generous window since signals fire infrequently
# and laggard tokens may only be cached once per narrative cycle (30 min+).
_MAX_PRICE_AGE_SECONDS = 3600  # 1 hour for paper; tighten for live


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
    """

    def __init__(self, mode: str, db: Database, settings) -> None:
        self.mode = mode
        self.db = db
        self.settings = settings
        self._paper_trader = PaperTrader()

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
    ) -> int | None:
        """Open a new trade. Returns trade_id or None if rejected.

        If *entry_price* is provided (and > 0), it is used directly instead of
        looking up price_cache.  This is essential for trending/gainers/losers
        signals where the snapshot already contains a fresh price.
        """
        if signal_data is None:
            signal_data = {}

        conn = self.db._conn
        if conn is None:
            raise RuntimeError("Database not initialized.")

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

        # 2. Check duplicate open position
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM paper_trades WHERE token_id = ? AND status = 'open'",
            (token_id,),
        )
        row = await cursor.fetchone()
        if row[0] > 0:
            log.info("trade_skipped_duplicate", token_id=token_id)
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

        # 4. Execute via paper trader
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
                sl_pct=self.settings.PAPER_SL_PCT,
                slippage_bps=self.settings.PAPER_SLIPPAGE_BPS,
            )
            return trade_id

        log.warning("trade_mode_not_supported", mode=self.mode)
        return None

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
        current_price = price_row[0]

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
        """Aggregate PnL statistics over the last N days."""
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
                 AND closed_at >= datetime('now', ?)""",
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
        """PnL breakdown by signal type."""
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
                 AND closed_at >= datetime('now', ?)
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
            updated_at = datetime.fromisoformat(str(row[1])).replace(tzinfo=timezone.utc)
            age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
            return (price, age_seconds)

        # Fallback: try fuzzy match by exact prefix (handles ID mismatches like bless-2 vs bless-network)
        cursor = await conn.execute(
            "SELECT coin_id, current_price, updated_at FROM price_cache WHERE coin_id LIKE ? LIMIT 1",
            (token_id + "%",),
        )
        row = await cursor.fetchone()
        if row is None or row[1] is None:
            return None

        matched_id = row[0]
        log.warning("price_fuzzy_match", requested=token_id, matched=matched_id)
        price = float(row[1])
        updated_at = datetime.fromisoformat(str(row[2])).replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - updated_at).total_seconds()
        return (price, age_seconds)
