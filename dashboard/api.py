"""Dashboard REST and WebSocket route handlers."""

import asyncio
import json
import os

import aiohttp as _aiohttp

# 5-minute cache for CoinGecko price enrichment (avoids hammering API on every poll)
_price_cache: dict = {}

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
        """Trending comparisons enriched with current CoinGecko prices."""
        from scout.db import Database as ScoutDatabase
        from scout.trending.tracker import get_recent_comparisons

        sdb = ScoutDatabase(_db_path)
        await sdb.initialize()
        try:
            comparisons = await get_recent_comparisons(sdb, limit=limit)
        finally:
            await sdb.close()

        if not comparisons:
            return comparisons

        # Collect CoinGecko coin IDs
        coin_ids = [c["coin_id"] for c in comparisons if c.get("coin_id")]
        if not coin_ids:
            return comparisons

        # Use /coins/markets (returns 24h + 7d changes) with 5-min cache
        import time as _time
        _now = _time.monotonic()
        if (
            _price_cache.get("_ts") is not None
            and _now - _price_cache["_ts"] < 300
            and _price_cache.get("_ids") == set(coin_ids)
        ):
            prices_map = _price_cache.get("_data", {})
        else:
            prices_map = {}
            try:
                ids_param = ",".join(coin_ids)
                # Use API key if available + rate limiter
                _headers = {}
                _cg_key = os.environ.get("COINGECKO_API_KEY", "")
                if _cg_key:
                    _headers["x-cg-demo-api-key"] = _cg_key
                try:
                    from scout.ratelimit import coingecko_limiter
                    await coingecko_limiter.acquire()
                except Exception:
                    pass
                async with _aiohttp.ClientSession() as session:
                    async with session.get(
                        "https://api.coingecko.com/api/v3/coins/markets",
                        params={
                            "vs_currency": "usd",
                            "ids": ids_param,
                            "sparkline": "false",
                            "price_change_percentage": "24h,7d",
                        },
                        headers=_headers,
                    ) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            for coin in data:
                                prices_map[coin["id"]] = {
                                    "usd": coin.get("current_price"),
                                    "change_24h": coin.get("price_change_percentage_24h"),
                                    "change_7d": coin.get("price_change_percentage_7d_in_currency"),
                                }
                _price_cache["_ts"] = _now
                _price_cache["_ids"] = set(coin_ids)
                _price_cache["_data"] = prices_map
            except Exception:
                pass  # Degrade gracefully

        for c in comparisons:
            cid = c.get("coin_id", "")
            if cid in prices_map:
                c["price_current"] = prices_map[cid].get("usd")
                c["price_change_24h"] = prices_map[cid].get("change_24h")
                c["price_change_7d"] = prices_map[cid].get("change_7d")
            else:
                c["price_current"] = None
                c["price_change_24h"] = None
                c["price_change_7d"] = None

        return comparisons

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
