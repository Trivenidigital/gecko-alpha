"""Dashboard REST and WebSocket route handlers."""

import asyncio
import json
import os
from datetime import timedelta

import aiosqlite
import structlog
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from dashboard import db
from dashboard.models import (
    AlertResponse,
    CandidateResponse,
    FunnelResponse,
    SignalHitRate,
    StatusResponse,
    WinRateResponse,
)

_log = structlog.get_logger()

# BL-066': module-level Settings singleton (PR-review MF3 + ae6d0a SHOULD-FIX #1).
# Pydantic v2 BaseSettings re-reads .env + runs ~30 field validations on every
# instantiation (~5ms cold path). Calling Settings() per-request was a real
# regression on existing endpoints AND made the existing alerts endpoint
# fragile to .env state. Catch broadly so a misconfigured .env (or any other
# import-time crash in scout.config) doesn't take down the read-only dashboard
# at module import. structlog hoisted to top-of-file so the except clause
# can't itself fail on a deferred import.
try:
    from scout.config import Settings as _ScoutSettings

    _DASHBOARD_SETTINGS = _ScoutSettings()
except Exception as _e:  # pragma: no cover — paranoia for misconfigured .env
    _DASHBOARD_SETTINGS = None
    _log.error("dashboard_settings_init_failed", err=str(_e))

# BL-066' fallback constant — keep aligned with scout/config.py default.
_CAP_PER_DAY_FALLBACK = 5

# Default DB path — can be overridden via create_app()
# Note: _db_path is closure-captured via create_app() and safe for single-process use (L5).
_db_path: str = "scout.db"

# Cached ScoutDatabase instance — avoids re-creating + re-migrating on every request.
_scout_db = None


async def _get_scout_db(db_path: str):
    """Return a cached, initialized ScoutDatabase instance."""
    global _scout_db
    if _scout_db is None:
        from scout.db import Database as ScoutDatabase

        _scout_db = ScoutDatabase(db_path)
        await _scout_db.initialize()
    return _scout_db


def create_app(db_path: str | None = None) -> FastAPI:
    """Create the FastAPI application with the given DB path."""
    global _db_path
    if db_path is not None:
        _db_path = db_path

    app = FastAPI(title="Gecko-Alpha Dashboard")

    # --- REST endpoints ---

    @app.get("/api/candidates", response_model=list[CandidateResponse])
    async def get_candidates():
        return await db.get_candidates(_db_path, limit=20)

    @app.get("/api/alerts/recent", response_model=list[AlertResponse])
    async def get_alerts():
        return await db.get_recent_alerts(_db_path, limit=20)

    @app.get("/api/signals/today", response_model=list[SignalHitRate])
    async def get_signals():
        return await db.get_signal_hit_rates(_db_path)

    @app.get("/api/status", response_model=StatusResponse)
    async def get_status():
        return await db.get_status(_db_path)

    @app.get("/api/funnel/latest", response_model=FunnelResponse)
    async def get_funnel():
        return await db.get_funnel(_db_path)

    @app.get("/api/win-rate", response_model=WinRateResponse)
    async def get_win_rate():
        return await db.get_win_rate(_db_path)

    # --- Narrative rotation endpoints ---

    @app.get("/api/narrative/heating")
    async def get_narrative_heating():
        return await db.get_narrative_heating(_db_path)

    @app.get("/api/narrative/predictions")
    async def get_narrative_predictions(
        limit: int = Query(50, ge=1, le=500),
        outcome: str | None = Query(None),
    ):
        return await db.get_narrative_predictions(
            _db_path, limit=limit, outcome=outcome
        )

    @app.get("/api/narrative/metrics")
    async def get_narrative_metrics():
        return await db.get_narrative_metrics(_db_path)

    @app.get("/api/narrative/strategy")
    async def get_narrative_strategy():
        return await db.get_narrative_strategy(_db_path)

    class StrategyUpdate(BaseModel):
        value: str

    STRATEGY_BOUNDS = {
        "category_accel_threshold": (2.0, 15.0),
        "category_volume_growth_min": (5.0, 50.0),
        "laggard_max_mcap": (50_000_000, 1_000_000_000),
        "laggard_max_change": (5.0, 30.0),
        "laggard_min_change": (-50.0, 0.0),
        "laggard_min_volume": (10_000, 1_000_000),
        "hit_threshold_pct": (5.0, 50.0),
        "miss_threshold_pct": (-30.0, -5.0),
        "max_picks_per_category": (3, 10),
        "max_heating_per_cycle": (1, 10),
        "signal_cooldown_hours": (1, 12),
        "min_learn_sample": (50, 500),
        "min_trigger_count": (1, 10),
    }

    @app.put("/api/narrative/strategy/{key}")
    async def update_narrative_strategy(key: str, body: StrategyUpdate):
        from fastapi.responses import JSONResponse

        # Check if key is locked
        strategy_rows = await db.get_narrative_strategy(_db_path)
        row_map = {r["key"]: r for r in strategy_rows}
        if key not in row_map:
            return JSONResponse(
                status_code=404, content={"detail": f"Key '{key}' not found"}
            )
        if row_map[key].get("locked"):
            return JSONResponse(
                status_code=403, content={"detail": f"Key '{key}' is locked"}
            )

        # Bounds validation
        if key in STRATEGY_BOUNDS:
            lo, hi = STRATEGY_BOUNDS[key]
            try:
                numeric_val = float(body.value)
            except (ValueError, TypeError):
                return JSONResponse(
                    status_code=400,
                    content={"detail": f"Value for '{key}' must be numeric"},
                )
            if numeric_val < lo or numeric_val > hi:
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": f"Value for '{key}' must be between {lo} and {hi}"
                    },
                )

        result = await db.update_narrative_strategy(_db_path, key, body.value)
        if result is None:
            return JSONResponse(
                status_code=404, content={"detail": f"Key '{key}' not found"}
            )
        return result

    @app.get("/api/narrative/learn-logs")
    async def get_narrative_learn_logs(limit: int = Query(20, ge=1, le=200)):
        return await db.get_narrative_learn_logs(_db_path, limit=limit)

    @app.get("/api/narrative/categories/history")
    async def get_narrative_category_history(
        category_id: str = Query(...),
        hours: int = Query(48, ge=1, le=720),
    ):
        return await db.get_narrative_category_history(
            _db_path, category_id=category_id, hours=hours
        )

    # --- Quality signals endpoint ---

    @app.get("/api/signals/quality")
    async def get_quality_signals(
        max_mcap: float = Query(200_000_000, ge=0),
        limit: int = Query(30, ge=1, le=200),
    ):
        """High-quality signals -- curated, enriched, filtered."""
        return await db.get_quality_signals(_db_path, max_mcap=max_mcap, limit=limit)

    # --- Paper trading endpoints ---

    @app.get("/api/trading/positions")
    async def get_trading_positions_endpoint():
        return await db.get_trading_positions(_db_path)

    @app.get("/api/trading/history")
    async def get_trading_history_endpoint(
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        return await db.get_trading_history(_db_path, limit=limit, offset=offset)

    @app.get("/api/trading/history/count")
    async def get_trading_history_count_endpoint():
        """Total count of closed paper trades — for frontend pagination."""
        return {"total": await db.get_trading_history_count(_db_path)}

    @app.get("/api/trading/stats")
    async def get_trading_stats_endpoint(
        days: int = Query(7, ge=1, le=365),
    ):
        return await db.get_trading_stats(_db_path, days=days)

    @app.post("/api/trading/close/{trade_id}")
    async def close_trade(trade_id: int):
        """Manually close a paper trade.

        No auth required -- paper trading uses simulated money.
        Double-click protection: checks trade is still open before processing.
        """
        from fastapi.responses import JSONResponse
        from scout.trading.paper import PaperTrader

        sdb = await _get_scout_db(_db_path)
        # Double-click protection: verify trade exists and is still open
        cursor = await sdb._conn.execute(
            "SELECT token_id, status FROM paper_trades WHERE id = ?", (trade_id,)
        )
        row = await cursor.fetchone()
        if not row:
            return JSONResponse(status_code=404, content={"error": "Trade not found"})
        if row["status"] != "open":
            return JSONResponse(
                status_code=400, content={"error": "Trade already closed"}
            )

        token_id = row["token_id"]
        pc = await sdb._conn.execute(
            "SELECT current_price FROM price_cache WHERE coin_id = ?", (token_id,)
        )
        price_row = await pc.fetchone()
        current_price = price_row[0] if price_row else 0

        trader = PaperTrader()
        await trader.execute_sell(
            db=sdb,
            trade_id=trade_id,
            current_price=current_price,
            reason="manual",
            slippage_bps=50,
        )
        return {"ok": True, "trade_id": trade_id}

    @app.get("/api/trading/stats/by-signal")
    async def get_trading_stats_by_signal_endpoint(
        days: int = Query(7, ge=1, le=365),
    ):
        return await db.get_trading_stats_by_signal(_db_path, days=days)

    # --- Preferences endpoints ---

    @app.get("/api/preferences/categories")
    async def list_available_categories():
        """List all category IDs from recent snapshots for preference selection."""
        return await db.get_available_categories(_db_path)

    # --- Second-Wave Detection endpoints ---

    @app.get("/api/secondwave/candidates")
    async def secondwave_candidates(days: int = 7, limit: int = 50):
        sdb = await _get_scout_db(_db_path)
        rows = await sdb.get_recent_secondwave_candidates(days=days)
        return rows[:limit]

    @app.get("/api/secondwave/stats")
    async def secondwave_stats(days: int = 7):
        sdb = await _get_scout_db(_db_path)
        rows = await sdb.get_recent_secondwave_candidates(days=days)
        count = len(rows)
        avg_score = (
            sum(r["reaccumulation_score"] for r in rows) / count if count else 0.0
        )
        return {"count": count, "avg_score": round(avg_score, 1), "days": days}

    # --- Chains endpoints ---

    @app.get("/api/chains/active")
    async def chains_active(limit: int = Query(50, ge=1, le=500)):
        return await db.get_chains_active(_db_path, limit=limit)

    @app.get("/api/chains/matches")
    async def chains_matches(limit: int = Query(30, ge=1, le=500)):
        return await db.get_chains_matches(_db_path, limit=limit)

    @app.get("/api/chains/patterns")
    async def chains_patterns():
        return await db.get_chains_patterns(_db_path)

    @app.get("/api/chains/events/recent")
    async def chains_events_recent(limit: int = Query(50, ge=1, le=500)):
        return await db.get_chains_events_recent(_db_path, limit=limit)

    @app.get("/api/chains/top-movers")
    async def chains_top_movers(limit: int = Query(5, ge=1, le=20)):
        return await db.get_chains_top_movers(_db_path, limit=limit)

    @app.get("/api/chains/stats")
    async def chains_stats():
        return await db.get_chains_stats(_db_path)

    # --- Trending Tracker endpoints ---

    @app.get("/api/trending/snapshots")
    async def trending_snapshots(
        hours: int = Query(24, ge=1, le=168), limit: int = Query(100, ge=1, le=500)
    ):
        from scout.trending.tracker import get_recent_snapshots

        sdb = await _get_scout_db(_db_path)
        return await get_recent_snapshots(sdb, hours=hours, limit=limit)

    @app.get("/api/trending/stats")
    async def trending_stats():
        from scout.trending.tracker import get_trending_stats

        sdb = await _get_scout_db(_db_path)
        stats = await get_trending_stats(sdb)
        return stats.model_dump()

    @app.get("/api/trending/comparisons")
    async def trending_comparisons(limit: int = Query(100, ge=1, le=500)):
        from scout.trending.tracker import get_recent_comparisons

        sdb = await _get_scout_db(_db_path)
        return await get_recent_comparisons(sdb, limit=limit)

    @app.get("/api/trending/comparisons-enriched")
    async def trending_comparisons_enriched(limit: int = Query(30, ge=1, le=500)):
        """Trending comparisons enriched with cached price data from the pipeline.

        Reads from the price_cache table (populated during pipeline ingestion)
        instead of calling CoinGecko directly, avoiding 429 rate-limit errors.
        """
        from scout.trending.tracker import get_recent_comparisons

        sdb = await _get_scout_db(_db_path)
        comparisons = await get_recent_comparisons(sdb, limit=limit)
        if not comparisons:
            return comparisons

        # Collect CoinGecko coin IDs
        coin_ids = [c["coin_id"] for c in comparisons if c.get("coin_id")]
        if not coin_ids:
            return comparisons

        # Read from price_cache table (populated by pipeline)
        prices_map = await sdb.get_cached_prices(coin_ids)

        # Also build a symbol-based lookup for ID mismatches (e.g. bless-network vs bless-2)
        symbols = [c.get("symbol", "").lower() for c in comparisons if c.get("symbol")]
        if symbols:
            try:
                cursor = await sdb._conn.execute(
                    "SELECT coin_id, current_price, price_change_24h, price_change_7d, market_cap FROM price_cache"
                )
                all_prices = await cursor.fetchall()
                symbol_map = {}
                for row in all_prices:
                    symbol_map[row["coin_id"]] = {
                        "usd": row["current_price"],
                        "change_24h": row["price_change_24h"],
                        "change_7d": row["price_change_7d"],
                        "market_cap": row["market_cap"],
                    }
            except Exception:
                symbol_map = {}

        # Look up earliest detection price for each coin from predictions/candidates
        detection_prices: dict = {}
        for c in comparisons:
            cid = c.get("coin_id", "")
            sym = (c.get("symbol") or "").upper()
            # Try predictions table first (has entry_price or market_cap_at_prediction)
            try:
                cursor = await sdb._conn.execute(
                    """SELECT market_cap_at_prediction FROM predictions
                       WHERE (coin_id = ? OR UPPER(symbol) = ?)
                       ORDER BY predicted_at ASC LIMIT 1""",
                    (cid, sym),
                )
                prow = await cursor.fetchone()
                if prow and prow[0]:
                    detection_prices[cid] = {"mcap": prow[0]}
            except Exception:
                pass

        for c in comparisons:
            cid = c.get("coin_id", "")
            sym = (c.get("symbol") or "").lower()
            matched = prices_map.get(cid)
            # Fallback: search price_cache by coin_id containing the symbol
            if not matched and sym:
                for pcid, pdata in symbol_map.items():
                    if pcid.startswith(sym) or sym in pcid:
                        matched = pdata
                        break
            if matched:
                c["price_current"] = matched.get("usd")
                c["price_change_24h"] = matched.get("change_24h")
                c["price_change_7d"] = matched.get("change_7d")
                c["market_cap"] = matched.get("market_cap")
            else:
                c["price_current"] = None
                c["price_change_24h"] = None
                c["price_change_7d"] = None
                c["market_cap"] = None

            # Use stored detected_price if available, otherwise estimate
            if c.get("detected_price"):
                c["price_at_detection"] = c["detected_price"]
            elif c.get("price_current") and c.get("price_change_24h"):
                change = c["price_change_24h"]
                if change > -100:
                    c["price_at_detection"] = c["price_current"] / (1 + change / 100)
                else:
                    c["price_at_detection"] = None
            else:
                c["price_at_detection"] = None

        return comparisons

    # --- Volume Spikes endpoints ---

    @app.get("/api/spikes/recent")
    async def spikes_recent(limit: int = Query(20, ge=1, le=200)):
        """Recent volume spikes."""
        from scout.spikes.detector import get_recent_spikes

        sdb = await _get_scout_db(_db_path)
        return await get_recent_spikes(sdb, limit=limit)

    @app.get("/api/spikes/stats")
    async def spikes_stats():
        """Spike detection stats."""
        from scout.spikes.detector import get_spike_stats

        sdb = await _get_scout_db(_db_path)
        return await get_spike_stats(sdb)

    # --- 7-Day Momentum Scanner endpoints ---

    @app.get("/api/momentum/7d")
    async def momentum_7d_recent(limit: int = Query(20, ge=1, le=200)):
        """Tokens with extreme 7d returns detected by the momentum scanner."""
        from scout.spikes.detector import get_recent_momentum_7d

        sdb = await _get_scout_db(_db_path)
        return await get_recent_momentum_7d(sdb, limit=limit)

    @app.get("/api/momentum/7d/stats")
    async def momentum_7d_stats():
        """7d momentum scanner stats."""
        from scout.spikes.detector import get_momentum_7d_stats

        sdb = await _get_scout_db(_db_path)
        return await get_momentum_7d_stats(sdb)

    # --- Slow-Burn Watcher endpoints (BL-075 Phase B) ---

    @app.get("/api/slow_burn")
    async def slow_burn_recent(limit: int = Query(20, ge=1, le=200)):
        """Recent slow-burn detections (research-only)."""
        from scout.spikes.detector import get_recent_slow_burn

        sdb = await _get_scout_db(_db_path)
        return await get_recent_slow_burn(sdb, limit=limit)

    @app.get("/api/slow_burn/stats")
    async def slow_burn_stats():
        """Slow-burn stats incl. D+14 soak gate values (volume / mcap-unknown
        cohort split / momentum_7d overlap %)."""
        from scout.spikes.detector import get_slow_burn_stats

        sdb = await _get_scout_db(_db_path)
        return await get_slow_burn_stats(sdb)

    # --- Top Gainers Tracker endpoints ---

    @app.get("/api/gainers/snapshots")
    async def gainers_snapshots(limit: int = Query(20, ge=1, le=200)):
        """Recent top gainers snapshots."""
        from scout.gainers.tracker import get_recent_gainers

        sdb = await _get_scout_db(_db_path)
        return await get_recent_gainers(sdb, limit=limit)

    @app.get("/api/gainers/comparisons")
    async def gainers_comparisons(limit: int = Query(50, ge=1, le=500)):
        """Gainers comparisons enriched with price_cache data."""
        from scout.gainers.tracker import get_gainers_comparisons

        sdb = await _get_scout_db(_db_path)
        comparisons = await get_gainers_comparisons(sdb, limit=limit)
        if not comparisons:
            return comparisons

        # Collect coin IDs for price_cache lookup
        coin_ids = [c["coin_id"] for c in comparisons if c.get("coin_id")]
        prices_map = await sdb.get_cached_prices(coin_ids) if coin_ids else {}

        # Build symbol fallback map
        symbol_map: dict = {}
        try:
            cursor = await sdb._conn.execute(
                "SELECT coin_id, current_price, price_change_24h, price_change_7d, market_cap FROM price_cache"
            )
            all_prices = await cursor.fetchall()
            for row in all_prices:
                symbol_map[row["coin_id"]] = {
                    "usd": row["current_price"],
                    "change_24h": row["price_change_24h"],
                    "change_7d": row["price_change_7d"],
                    "market_cap": row["market_cap"],
                }
        except Exception:
            pass

        # Look up price_at_snapshot from gainers_snapshots for each coin
        price_at_snap: dict = {}
        for c in comparisons:
            cid = c.get("coin_id", "")
            try:
                cursor = await sdb._conn.execute(
                    """SELECT price_at_snapshot, price_change_24h
                       FROM gainers_snapshots
                       WHERE coin_id = ?
                       ORDER BY snapshot_at ASC LIMIT 1""",
                    (cid,),
                )
                row = await cursor.fetchone()
                if row and row["price_at_snapshot"]:
                    price_at_snap[cid] = row["price_at_snapshot"]
                elif row:
                    # Estimate: no price_at_snapshot stored yet, back-calculate
                    # from current price and the 24h change at time of snapshot
                    pass
            except Exception:
                pass

        for c in comparisons:
            cid = c.get("coin_id", "")
            sym = (c.get("symbol") or "").lower()
            matched = prices_map.get(cid)
            if not matched and sym:
                for pcid, pdata in symbol_map.items():
                    if pcid.startswith(sym) or sym in pcid:
                        matched = pdata
                        break
            if matched:
                c["price_current"] = matched.get("usd")
                c["price_change_7d"] = matched.get("change_7d")
                c["market_cap"] = matched.get("market_cap") or c.get("market_cap")
            else:
                c["price_current"] = None
                c["price_change_7d"] = None

            # price_at_detection: prefer stored detected_price, then snapshot price, then estimate
            if c.get("detected_price"):
                c["price_at_detection"] = c["detected_price"]
            elif price_at_snap.get(cid):
                c["price_at_detection"] = price_at_snap[cid]
            elif c.get("price_current") and c.get("price_change_24h"):
                change = c["price_change_24h"]
                if change > -100:
                    c["price_at_detection"] = c["price_current"] / (1 + change / 100)
                else:
                    c["price_at_detection"] = None
            else:
                c["price_at_detection"] = None

        return comparisons

    @app.get("/api/gainers/stats")
    async def gainers_stats():
        """Gainers tracker hit rate stats."""
        from scout.gainers.tracker import get_gainers_stats

        sdb = await _get_scout_db(_db_path)
        return await get_gainers_stats(sdb)

    # --- Losers Tracker ---

    @app.get("/api/losers/comparisons")
    async def losers_comparisons(limit: int = Query(50, ge=1, le=500)):
        """Losers comparisons with signal detection."""
        from scout.losers.tracker import get_losers_comparisons

        sdb = await _get_scout_db(_db_path)
        return await get_losers_comparisons(sdb, limit=limit)

    @app.get("/api/losers/stats")
    async def losers_stats():
        """Losers tracker hit rate stats."""
        from scout.losers.tracker import get_losers_stats

        sdb = await _get_scout_db(_db_path)
        return await get_losers_stats(sdb)

    # --- Briefing endpoints ---

    # Manual briefing cooldown tracking (5-minute cooldown)
    _last_manual_briefing_at: dict = {"ts": None}

    @app.get("/api/briefing/latest")
    async def briefing_latest():
        """Most recent briefing text + metadata."""
        result = await db.get_briefing_latest(_db_path)
        if result is None:
            return {"briefing": None}
        return result

    @app.get("/api/briefing/history")
    async def briefing_history(limit: int = Query(10, ge=1, le=50)):
        """Past briefings."""
        return await db.get_briefing_history(_db_path, limit=limit)

    @app.post("/api/briefing/generate")
    async def briefing_generate():
        """Manually trigger a briefing (5-minute cooldown)."""
        from fastapi.responses import JSONResponse
        from datetime import datetime, timezone as tz

        now = datetime.now(tz.utc)

        # 5-minute cooldown
        if _last_manual_briefing_at["ts"] is not None:
            elapsed = (now - _last_manual_briefing_at["ts"]).total_seconds()
            if elapsed < 300:
                remaining = int(300 - elapsed)
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": f"Cooldown: wait {remaining}s before next manual briefing"
                    },
                )

        try:
            sdb = await _get_scout_db(_db_path)
            from scout.config import get_settings

            settings = get_settings()

            if not settings.ANTHROPIC_API_KEY:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "ANTHROPIC_API_KEY not configured"},
                )

            import aiohttp as _aio
            from scout.briefing.collector import collect_briefing_data
            from scout.briefing.synthesizer import synthesize_briefing
            import json as _json

            async with _aio.ClientSession() as session:
                raw = await collect_briefing_data(session, sdb, settings)
                synthesis = await synthesize_briefing(
                    raw, settings.ANTHROPIC_API_KEY, settings.BRIEFING_MODEL
                )

            bid = await db.store_briefing(
                _db_path,
                briefing_type="manual",
                raw_data=_json.dumps(raw, default=str),
                synthesis=synthesis,
                model_used=settings.BRIEFING_MODEL,
                created_at=now.isoformat(),
            )
            _last_manual_briefing_at["ts"] = now
            return {"id": bid, "synthesis": synthesis, "created_at": now.isoformat()}
        except Exception:
            return JSONResponse(
                status_code=500, content={"detail": "Briefing generation failed"}
            )

    @app.get("/api/briefing/schedule")
    async def briefing_schedule():
        """Next scheduled briefing time."""
        from datetime import datetime, timezone as tz
        from scout.config import get_settings

        settings = get_settings()
        now = datetime.now(tz.utc)
        hours = [int(h.strip()) for h in settings.BRIEFING_HOURS_UTC.split(",")]
        hours.sort()

        # Find next scheduled hour
        next_hour = None
        for h in hours:
            if h > now.hour:
                next_hour = h
                break
        if next_hour is None:
            next_hour = hours[0]  # wrap to tomorrow

        if next_hour > now.hour:
            next_time = now.replace(hour=next_hour, minute=0, second=0, microsecond=0)
        else:
            next_time = (now + timedelta(days=1)).replace(
                hour=next_hour, minute=0, second=0, microsecond=0
            )

        last_time = await db.get_last_briefing_time(_db_path)

        return {
            "enabled": settings.BRIEFING_ENABLED,
            "hours_utc": hours,
            "next_scheduled": next_time.isoformat(),
            "last_briefing_at": last_time,
            "model": settings.BRIEFING_MODEL,
        }

    # --- System health endpoint ---

    @app.get("/api/system/health")
    async def system_health():
        return await db.get_system_health(_db_path)

    @app.get("/health")
    async def health_check():
        """Health check endpoint for uptime monitoring."""
        db_ok = False
        last_cycle = None
        pipeline_running = False
        try:
            async with db._ro_db(_db_path) as conn:
                db_ok = True
                cursor = await conn.execute("SELECT MAX(first_seen_at) FROM candidates")
                row = await cursor.fetchone()
                last_cycle = row[0] if row and row[0] else None
                if last_cycle:
                    from datetime import datetime, timezone

                    last_dt = datetime.fromisoformat(last_cycle.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - last_dt).total_seconds()
                    pipeline_running = age < 180  # 3x 60s scan interval
        except Exception:
            pass
        return {
            "status": "ok" if db_ok else "degraded",
            "pipeline_running": pipeline_running,
            "last_cycle_at": last_cycle,
            "db_reachable": db_ok,
        }

    # --- WebSocket ---

    _ws_clients: set[WebSocket] = set()

    @app.websocket("/ws/live")
    async def websocket_live(ws: WebSocket):
        await ws.accept()
        _ws_clients.add(ws)
        try:
            while True:
                # Poll DB every 5 seconds and push updates
                try:
                    status = await db.get_status(_db_path)
                    candidates = await db.get_candidates(_db_path, limit=20)
                    funnel = await db.get_funnel(_db_path)
                    signals = await db.get_signal_hit_rates(_db_path)
                    alerts = await db.get_recent_alerts(_db_path, limit=20)

                    payload = json.dumps(
                        {
                            "type": "update",
                            "status": status,
                            "candidates": candidates,
                            "funnel": funnel,
                            "signals": signals,
                            "alerts": alerts,
                        },
                        default=str,
                    )

                    await ws.send_text(payload)
                except Exception:
                    pass  # DB may not exist yet -- keep connection alive

                await asyncio.sleep(5)
        except WebSocketDisconnect:
            pass
        finally:
            _ws_clients.discard(ws)

    @app.get("/api/tg_social/alerts")
    async def get_tg_social_alerts(limit: int = 50):
        """BL-064 visibility surface: recent messages joined with their
        resolved signal (if any) and channel config. Until the Telegram
        bot token is fixed this is the only operator-facing view of TG
        cashtag/contract activity. See backlog item BL-066.
        """
        scout_db = await _get_scout_db(_db_path)
        conn = scout_db._conn

        # BL-066': defensive fallback for the F19 startup race + F5 rollback
        # scenarios (PR-review MF1 — narrow the substring catch so it doesn't
        # swallow unrelated OperationalErrors that mention the column name).
        # The cashtag_trade_eligible column was added in BL-065 (master >=
        # 835ce7f); if dashboard reads the DB before pipeline finishes its
        # migration OR if DB is rolled back to pre-BL-065 while dashboard
        # stays current, the new SELECT shape would 500. Catch only the
        # specific "no such column" form; anything else (syntax error,
        # ambiguous column from a JOIN refactor) re-raises so the bug stays
        # visible. T11 pins this path.
        try:
            ch_cur = await conn.execute(
                """SELECT channel_handle, trade_eligible, safety_required,
                          cashtag_trade_eligible, removed_at, added_at
                   FROM tg_social_channels ORDER BY added_at"""
            )
            ch_rows = await ch_cur.fetchall()
            _has_cashtag_col = True
        except aiosqlite.OperationalError as exc:
            _msg = str(exc)
            if "no such column" not in _msg or "cashtag_trade_eligible" not in _msg:
                raise
            _log.warning(
                "dashboard_cashtag_column_missing_fallback",
                err=_msg,
            )
            ch_cur = await conn.execute(
                """SELECT channel_handle, trade_eligible, safety_required,
                          removed_at, added_at
                   FROM tg_social_channels ORDER BY added_at"""
            )
            ch_rows = [
                (r[0], r[1], r[2], 0, r[3], r[4]) for r in await ch_cur.fetchall()
            ]
            _has_cashtag_col = False

        # BL-066': per-channel cashtag dispatches today (calendar-day,
        # mirrors dispatcher.py:_channel_cashtag_trades_today_count).
        cashtag_today = (
            await db.get_tg_social_per_channel_cashtag_today(_db_path)
            if _has_cashtag_col
            else {}
        )
        # BL-066': cap_per_day from cached module-level Settings singleton
        # (PR-review MF3 — log at REQUEST time when fallback active so the
        # failure isn't silent. Without this, a misconfigured .env at process
        # start would make every dashboard call silently use cap=5 even if
        # operator's real cap is 10, with only the import-time log to show
        # for it).
        if _DASHBOARD_SETTINGS is None:
            _log.error(
                "dashboard_cap_per_day_fallback_active",
                endpoint="/api/tg_social/alerts",
                fallback=_CAP_PER_DAY_FALLBACK,
            )
            cap_per_day = _CAP_PER_DAY_FALLBACK
        else:
            cap_per_day = (
                _DASHBOARD_SETTINGS.PAPER_TG_SOCIAL_CASHTAG_MAX_PER_CHANNEL_PER_DAY
            )

        channels = [
            {
                "channel_handle": r[0],
                "trade_eligible": bool(r[1]),
                "safety_required": bool(r[2]),
                "cashtag_trade_eligible": bool(r[3]),
                "cashtag_dispatched_today": cashtag_today.get(r[0], 0),
                "cashtag_cap_per_day": cap_per_day,
                "removed": r[4] is not None,
                "added_at": r[5],
            }
            for r in ch_rows
        ]

        health_cur = await conn.execute(
            "SELECT component, listener_state, last_message_at, updated_at "
            "FROM tg_social_health"
        )
        health = {
            r[0]: {
                "state": r[1],
                "last_message_at": r[2],
                "updated_at": r[3],
            }
            for r in await health_cur.fetchall()
        }

        # Messages joined to their resolution (left join — many messages
        # have no signal row when the parser found nothing tradeable).
        msg_cur = await conn.execute(
            """SELECT m.id, m.channel_handle, m.msg_id, m.posted_at,
                      m.sender, m.text, m.cashtags, m.contracts,
                      s.token_id, s.symbol, s.contract_address, s.chain,
                      s.mcap_at_sighting, s.resolution_state, s.paper_trade_id
               FROM tg_social_messages m
               LEFT JOIN tg_social_signals s ON s.message_pk = m.id
               ORDER BY m.posted_at DESC
               LIMIT ?""",
            (max(1, min(limit, 200)),),
        )
        alerts = []
        for r in await msg_cur.fetchall():
            text = r[5] or ""
            alerts.append(
                {
                    "id": r[0],
                    "channel_handle": r[1],
                    "msg_id": r[2],
                    "posted_at": r[3],
                    "sender": r[4],
                    "text_preview": text[:240],
                    "cashtags": r[6],
                    "contracts": r[7],
                    "resolution": (
                        {
                            "token_id": r[8],
                            "symbol": r[9],
                            "contract_address": r[10],
                            "chain": r[11],
                            "mcap": r[12],
                            "state": r[13],
                            "paper_trade_id": r[14],
                        }
                        if r[8] is not None or r[13] is not None
                        else None
                    ),
                }
            )

        # 24h rollup
        stats_cur = await conn.execute("""SELECT
                 COUNT(*) AS msgs,
                 SUM(CASE WHEN contracts NOT IN ('','[]') THEN 1 ELSE 0 END) AS with_ca,
                 SUM(CASE WHEN cashtags NOT IN ('','[]') THEN 1 ELSE 0 END) AS with_cashtag
               FROM tg_social_messages
               WHERE datetime(posted_at) >= datetime('now', '-24 hours')""")
        s = await stats_cur.fetchone()
        sig_cur = await conn.execute("""SELECT
                 COUNT(*) AS sigs,
                 SUM(CASE WHEN paper_trade_id IS NOT NULL THEN 1 ELSE 0 END) AS dispatched
               FROM tg_social_signals
               WHERE datetime(created_at) >= datetime('now', '-24 hours')""")
        sig = await sig_cur.fetchone()
        dlq_cur = await conn.execute(
            "SELECT COUNT(*) FROM tg_social_dlq "
            "WHERE datetime(failed_at) >= datetime('now', '-24 hours')"
        )
        dlq = (await dlq_cur.fetchone())[0]

        # BL-066': cashtag-dispatch 24h rollup (rolling window — different
        # surface from per-channel cap utilization, which is calendar-day).
        # PR-review SHOULD-FIX #4 (ae6d0a): named `cashtag_dispatched_24h` so
        # consumer can't accidentally compare against per-channel
        # `cashtag_dispatched_today` (calendar-day) and conclude the stats
        # are "inconsistent" — they use different windows by design.
        cashtag_stats = await db.get_tg_social_cashtag_stats_24h(_db_path)

        return {
            "channels": channels,
            "health": health,
            "stats_24h": {
                "messages": s[0] or 0,
                "with_ca": s[1] or 0,
                "with_cashtag": s[2] or 0,
                "signals_resolved": sig[0] or 0,
                "trades_dispatched": sig[1] or 0,
                "cashtag_dispatched_24h": cashtag_stats["dispatched"],
                "dlq": dlq,
            },
            "alerts": alerts,
            # PR-review MF3 (a707628): expose Settings init state so the
            # frontend can render an honest banner if the cap_per_day shown
            # is the hard-coded fallback rather than operator-configured.
            "settings_loaded": _DASHBOARD_SETTINGS is not None,
        }

    @app.get("/api/tg_social/dlq")
    async def get_tg_social_dlq_endpoint(
        limit: int = Query(20, ge=1, le=100),
    ):
        """BL-066' DLQ inspector. Recent failures with truncated raw_text.

        DLQ row schema: (channel_handle, msg_id, raw_text, error_class,
        error_text, failed_at, retried_at). Last entry as of 2026-05-04
        was 2026-04-28 (post-PR #55 listener resilience deploy stabilized
        the listener); empty-state expected to be the common case.

        Split from /api/tg_social/alerts because: (1) DLQ rows carry
        ~240-char raw_text payloads — coupling to 15s-poll composite
        alerts response would inflate every poll with ~empty data;
        (2) ?limit= parameterization is natural here (operator scrolling
        failures) but awkward on composite endpoint where alerts/channels/
        health/stats have different natural sizes; (3) DLQ refresh cadence
        is slower (30s in TGDLQPanel vs 15s in TGAlertsTab) — combining
        would force the slower cadence on the hot stats panel.
        """
        return await db.get_tg_social_dlq(_db_path, limit=limit)

    @app.get("/api/signal_params")
    async def get_signal_params():
        """Per-signal params + rolling 30d performance + flag state.

        ``effective_source`` is "settings" when SIGNAL_PARAMS_ENABLED=False
        (table values shown but NOT in use) — the frontend renders a
        warning banner in that case so the operator never thinks
        post-calibration values are live when they aren't.
        """
        from datetime import datetime, timedelta, timezone

        from scout.config import Settings

        settings = Settings()
        scout_db = await _get_scout_db(_db_path)
        conn = scout_db._conn
        flag_enabled = bool(settings.SIGNAL_PARAMS_ENABLED)

        cur = await conn.execute(
            """SELECT signal_type, leg_1_pct, leg_1_qty_frac, leg_2_pct,
                      leg_2_qty_frac, trail_pct, trail_pct_low_peak,
                      low_peak_threshold_pct, sl_pct, max_duration_hours,
                      enabled, suspended_at, suspended_reason,
                      last_calibration_at, last_calibration_reason
               FROM signal_params ORDER BY signal_type"""
        )
        rows = await cur.fetchall()

        since_iso = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        params = []
        for r in rows:
            stat_cur = await conn.execute(
                """SELECT COUNT(*),
                          COALESCE(SUM(pnl_usd), 0),
                          SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END)
                   FROM paper_trades
                   WHERE signal_type = ?
                     AND status LIKE 'closed_%'
                     AND datetime(closed_at) >= datetime(?)""",
                (r[0], since_iso),
            )
            n, net, wins = await stat_cur.fetchone()
            n = n or 0
            wins = wins or 0
            win_pct = round(100.0 * wins / n, 1) if n > 0 else 0.0
            params.append(
                {
                    "signal_type": r[0],
                    "leg_1_pct": r[1],
                    "leg_1_qty_frac": r[2],
                    "leg_2_pct": r[3],
                    "leg_2_qty_frac": r[4],
                    "trail_pct": r[5],
                    "trail_pct_low_peak": r[6],
                    "low_peak_threshold_pct": r[7],
                    "sl_pct": r[8],
                    "max_duration_hours": r[9],
                    "enabled": bool(r[10]),
                    "suspended_at": r[11],
                    "suspended_reason": r[12],
                    "last_calibration_at": r[13],
                    "last_calibration_reason": r[14],
                    "effective_source": "table" if flag_enabled else "settings",
                    "rolling_30d": {
                        "trades": n,
                        "net_pnl": round(float(net or 0), 2),
                        "win_pct": win_pct,
                    },
                }
            )

        audit_cur = await conn.execute(
            """SELECT signal_type, field_name, old_value, new_value,
                      reason, applied_by, applied_at
               FROM signal_params_audit
               ORDER BY applied_at DESC LIMIT 10"""
        )
        recent = [
            {
                "signal_type": a[0],
                "field_name": a[1],
                "old_value": a[2],
                "new_value": a[3],
                "reason": a[4],
                "applied_by": a[5],
                "applied_at": a[6],
            }
            for a in await audit_cur.fetchall()
        ]

        return {
            "flag_enabled": flag_enabled,
            "params": params,
            "recent_changes": recent,
        }

    # --- Static files (mounted last so API routes take priority) ---
    dist_dir = os.path.join(os.path.dirname(__file__), "frontend", "dist")
    if os.path.isdir(dist_dir):
        app.mount("/", StaticFiles(directory=dist_dir, html=True), name="frontend")

    return app
