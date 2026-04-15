"""Dashboard REST and WebSocket route handlers."""

import asyncio
import json
import os

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

# Default DB path — can be overridden via create_app()
_db_path: str = "scout.db"


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
        return await db.get_narrative_predictions(_db_path, limit=limit, outcome=outcome)

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
            return JSONResponse(status_code=404, content={"detail": f"Key '{key}' not found"})
        if row_map[key].get("locked"):
            return JSONResponse(status_code=403, content={"detail": f"Key '{key}' is locked"})

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
                    content={"detail": f"Value for '{key}' must be between {lo} and {hi}"},
                )

        result = await db.update_narrative_strategy(_db_path, key, body.value)
        if result is None:
            return JSONResponse(status_code=404, content={"detail": f"Key '{key}' not found"})
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

    @app.get("/api/trading/stats")
    async def get_trading_stats_endpoint(
        days: int = Query(7, ge=1, le=365),
    ):
        return await db.get_trading_stats(_db_path, days=days)

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
        from scout.db import Database as ScoutDatabase
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            rows = await sdb.get_recent_secondwave_candidates(days=days)
            return rows[:limit]
        finally:
            await sdb.close()

    @app.get("/api/secondwave/stats")
    async def secondwave_stats(days: int = 7):
        from scout.db import Database as ScoutDatabase
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            rows = await sdb.get_recent_secondwave_candidates(days=days)
            count = len(rows)
            avg_score = (
                sum(r["reaccumulation_score"] for r in rows) / count if count else 0.0
            )
            return {"count": count, "avg_score": round(avg_score, 1), "days": days}
        finally:
            await sdb.close()

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
    async def trending_snapshots(hours: int = Query(24, ge=1, le=168), limit: int = Query(100, ge=1, le=500)):
        from scout.db import Database as ScoutDatabase
        from scout.trending.tracker import get_recent_snapshots
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            return await get_recent_snapshots(sdb, hours=hours, limit=limit)
        finally:
            await sdb.close()

    @app.get("/api/trending/stats")
    async def trending_stats():
        from scout.db import Database as ScoutDatabase
        from scout.trending.tracker import get_trending_stats
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            stats = await get_trending_stats(sdb)
            return stats.model_dump()
        finally:
            await sdb.close()

    @app.get("/api/trending/comparisons")
    async def trending_comparisons(limit: int = Query(100, ge=1, le=500)):
        from scout.db import Database as ScoutDatabase
        from scout.trending.tracker import get_recent_comparisons
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            return await get_recent_comparisons(sdb, limit=limit)
        finally:
            await sdb.close()

    @app.get("/api/trending/comparisons-enriched")
    async def trending_comparisons_enriched(limit: int = Query(30, ge=1, le=500)):
        """Trending comparisons enriched with cached price data from the pipeline.

        Reads from the price_cache table (populated during pipeline ingestion)
        instead of calling CoinGecko directly, avoiding 429 rate-limit errors.
        """
        from scout.db import Database as ScoutDatabase
        from scout.trending.tracker import get_recent_comparisons

        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
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
                        "SELECT coin_id, current_price, price_change_24h, price_change_7d FROM price_cache"
                    )
                    all_prices = await cursor.fetchall()
                    symbol_map = {}
                    for row in all_prices:
                        # Extract likely symbol from coin_id (e.g. "genius-3" -> match by prefix)
                        symbol_map[row["coin_id"]] = {
                            "usd": row["current_price"],
                            "change_24h": row["price_change_24h"],
                            "change_7d": row["price_change_7d"],
                        }
                except Exception:
                    symbol_map = {}
        finally:
            await sdb.close()

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
            else:
                c["price_current"] = None
                c["price_change_24h"] = None
                c["price_change_7d"] = None

        return comparisons

    # --- Volume Spikes endpoints ---

    @app.get("/api/spikes/recent")
    async def spikes_recent(limit: int = Query(20, ge=1, le=200)):
        """Recent volume spikes."""
        from scout.db import Database as ScoutDatabase
        from scout.spikes.detector import get_recent_spikes
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            return await get_recent_spikes(sdb, limit=limit)
        finally:
            await sdb.close()

    @app.get("/api/spikes/stats")
    async def spikes_stats():
        """Spike detection stats."""
        from scout.db import Database as ScoutDatabase
        from scout.spikes.detector import get_spike_stats
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            return await get_spike_stats(sdb)
        finally:
            await sdb.close()

    # --- 7-Day Momentum Scanner endpoints ---

    @app.get("/api/momentum/7d")
    async def momentum_7d_recent(limit: int = Query(20, ge=1, le=200)):
        """Tokens with extreme 7d returns detected by the momentum scanner."""
        from scout.db import Database as ScoutDatabase
        from scout.spikes.detector import get_recent_momentum_7d
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            return await get_recent_momentum_7d(sdb, limit=limit)
        finally:
            await sdb.close()

    @app.get("/api/momentum/7d/stats")
    async def momentum_7d_stats():
        """7d momentum scanner stats."""
        from scout.db import Database as ScoutDatabase
        from scout.spikes.detector import get_momentum_7d_stats
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            return await get_momentum_7d_stats(sdb)
        finally:
            await sdb.close()

    # --- Top Gainers Tracker endpoints ---

    @app.get("/api/gainers/snapshots")
    async def gainers_snapshots(limit: int = Query(20, ge=1, le=200)):
        """Recent top gainers snapshots."""
        from scout.db import Database as ScoutDatabase
        from scout.gainers.tracker import get_recent_gainers
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            return await get_recent_gainers(sdb, limit=limit)
        finally:
            await sdb.close()

    @app.get("/api/gainers/comparisons")
    async def gainers_comparisons(limit: int = Query(50, ge=1, le=500)):
        """Gainers comparisons with signal detection."""
        from scout.db import Database as ScoutDatabase
        from scout.gainers.tracker import get_gainers_comparisons
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            return await get_gainers_comparisons(sdb, limit=limit)
        finally:
            await sdb.close()

    @app.get("/api/gainers/stats")
    async def gainers_stats():
        """Gainers tracker hit rate stats."""
        from scout.db import Database as ScoutDatabase
        from scout.gainers.tracker import get_gainers_stats
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            return await get_gainers_stats(sdb)
        finally:
            await sdb.close()

    # --- Losers Tracker ---

    @app.get("/api/losers/comparisons")
    async def losers_comparisons(limit: int = Query(50, ge=1, le=500)):
        """Losers comparisons with signal detection."""
        from scout.db import Database as ScoutDatabase
        from scout.losers.tracker import get_losers_comparisons
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            return await get_losers_comparisons(sdb, limit=limit)
        finally:
            await sdb.close()

    @app.get("/api/losers/stats")
    async def losers_stats():
        """Losers tracker hit rate stats."""
        from scout.db import Database as ScoutDatabase
        from scout.losers.tracker import get_losers_stats
        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            return await get_losers_stats(sdb)
        finally:
            await sdb.close()

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
                cursor = await conn.execute(
                    "SELECT MAX(first_seen_at) FROM candidates"
                )
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

                    payload = json.dumps({
                        "type": "update",
                        "status": status,
                        "candidates": candidates,
                        "funnel": funnel,
                        "signals": signals,
                        "alerts": alerts,
                    }, default=str)

                    await ws.send_text(payload)
                except Exception:
                    pass  # DB may not exist yet — keep connection alive

                await asyncio.sleep(5)
        except WebSocketDisconnect:
            pass
        finally:
            _ws_clients.discard(ws)

    # --- Static files (mounted last so API routes take priority) ---
    dist_dir = os.path.join(os.path.dirname(__file__), "frontend", "dist")
    if os.path.isdir(dist_dir):
        app.mount("/", StaticFiles(directory=dist_dir, html=True), name="frontend")

    return app
