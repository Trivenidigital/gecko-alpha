"""Dashboard REST and WebSocket route handlers."""

import asyncio
import json
import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

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
