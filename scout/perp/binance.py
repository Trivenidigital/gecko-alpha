"""Binance futures WS client + parser for perp anomaly detector."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import aiohttp
import structlog

from scout.config import Settings
from scout.perp.normalize import normalize_ticker
from scout.perp.schemas import PerpTick

if TYPE_CHECKING:
    pass  # ClassifierState imported only as a string annotation (Task 9)

logger = structlog.get_logger()


def parse_frame(frame: dict[str, Any]) -> list[PerpTick]:
    """Yield PerpTicks from a single Binance WS frame.

    Supports:
      * ``!markPrice@arr@1s`` — array of markPrice updates.
      * ``<symbol>@openInterest`` — single OI update.

    Malformed or unknown streams silently yield empty. Never raises.
    """
    ticks: list[PerpTick] = []
    stream = frame.get("stream") if isinstance(frame, dict) else None
    if stream and "markPrice@arr" in stream:
        data = frame.get("data") or []
        if isinstance(data, list):
            for item in data:
                tick = _parse_mark(item)
                if tick is not None:
                    ticks.append(tick)
    elif stream and "openInterest" in stream:
        data = frame.get("data") or {}
        if isinstance(data, dict):
            tick = _parse_oi(data)
            if tick is not None:
                ticks.append(tick)
    # OI and markPrice frames are snapshots of current value, not deltas;
    # drop-oldest in the queue is safe for both stream types.
    return ticks


def _parse_mark(item: dict[str, Any]) -> PerpTick | None:
    try:
        symbol = str(item.get("s", ""))
        ticker = normalize_ticker(symbol)
        if ticker is None:
            return None
        return PerpTick(
            exchange="binance",
            symbol=symbol,
            ticker=ticker,
            mark_price=float(item["p"]),
            funding_rate=float(item["r"]),
            timestamp=datetime.fromtimestamp(
                float(item.get("E", 0)) / 1000, tz=timezone.utc
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _parse_oi(item: dict[str, Any]) -> PerpTick | None:
    try:
        symbol = str(item.get("s", ""))
        ticker = normalize_ticker(symbol)
        if ticker is None:
            return None
        return PerpTick(
            exchange="binance",
            symbol=symbol,
            ticker=ticker,
            open_interest=float(item["o"]),
            timestamp=datetime.fromtimestamp(
                float(item.get("E", 0)) / 1000, tz=timezone.utc
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def stream_ticks(
    session: aiohttp.ClientSession,
    settings: Settings,
    state: "ClassifierState | None" = None,
) -> AsyncIterator[PerpTick]:
    """Open ONE Binance WS connection and yield PerpTicks until EOF/exception.

    Binance's server sends ping frames; aiohttp auto-replies pong. No
    outbound ping needed. Reconnect/backoff is NOT handled here -- the
    supervisor in scout/perp/watcher.py owns that concern (single-owner,
    injectable clock for tests). This coroutine either returns on clean
    close or lets exceptions propagate upward.

    The /stream endpoint subscribes via URL (?streams=...) so no
    SUBSCRIBE message is sent; frame shape on this endpoint is
    ``{"stream": "...", "data": {...}}`` which parse_frame already
    handles.
    """
    symbols = settings.PERP_SYMBOLS
    if not symbols:
        return
    streams = "/".join(
        ["!markPrice@arr@1s"] + [f"{s.lower()}@openInterest" for s in symbols]
    )
    url = f"{settings.PERP_BINANCE_WS_URL}?streams={streams}"
    async with session.ws_connect(
        url,
        headers=None,  # explicit: do not leak shared-session UA/auth headers
        heartbeat=settings.PERP_WS_PING_INTERVAL_SEC,
        max_msg_size=0,
    ) as ws:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                frame = json.loads(msg.data)
            except (ValueError, TypeError):
                if state is not None:
                    state.malformed_frames += 1
                continue
            for tick in parse_frame(frame):
                yield tick
