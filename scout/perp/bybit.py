"""Bybit v5 linear-perp WS client + parser."""

from __future__ import annotations

import asyncio
import contextlib
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
    from scout.perp.watcher import ClassifierState

log = structlog.get_logger(__name__)

WS_URL = "wss://stream.bybit.com/v5/public/linear"
_TICKER_TOPIC_PREFIX = "tickers."
# Bybit REQUIRES explicit JSON {"op": "ping"} every 20s. Different from
# Binance's server-sent ping; each client owns its own keepalive.
_SUBSCRIBE_ACK_TIMEOUT_SEC = 5.0


def parse_frame(
    frame: dict[str, Any],
    state: "ClassifierState | None" = None,
) -> list[PerpTick]:
    """Parse a single Bybit WS frame into PerpTicks.

    Per-item parse failures increment state.parse_rejects if state is provided.
    """
    if not isinstance(frame, dict):
        return []
    topic = frame.get("topic", "")
    if not isinstance(topic, str) or not topic.startswith(_TICKER_TOPIC_PREFIX):
        return []
    data = frame.get("data") or {}
    if not isinstance(data, dict):
        return []
    symbol = str(data.get("symbol", ""))
    ticker = normalize_ticker(symbol)
    if ticker is None:
        return []
    try:
        ts_ms = float(frame.get("ts", 0))
        tick = PerpTick(
            exchange="bybit",
            symbol=symbol,
            ticker=ticker,
            mark_price=(float(data["markPrice"]) if "markPrice" in data else None),
            funding_rate=(
                float(data["fundingRate"]) if "fundingRate" in data else None
            ),
            open_interest=(
                float(data["openInterest"]) if "openInterest" in data else None
            ),
            open_interest_usd=(
                float(data["openInterestValue"])
                if "openInterestValue" in data
                else None
            ),
            timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc),
        )
        return [tick]
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("perp.bybit.parse_failed", error=repr(exc), symbol=symbol)
        if state is not None:
            state.parse_rejects += 1
        return []


async def stream_ticks(
    session: aiohttp.ClientSession,
    settings: Settings,
    state: "ClassifierState | None" = None,
) -> AsyncIterator[PerpTick]:
    """Open ONE Bybit WS connection and yield PerpTicks until EOF/exception.

    Bybit REQUIRES explicit JSON {"op": "ping"} every 20s. Different from
    Binance's server-sent ping; each client owns its own keepalive.

    Reconnect/backoff is handled by the supervisor in
    scout/perp/watcher.py (single-owner, injectable clock). On empty
    PERP_SYMBOLS this returns early -- NEVER open a connection to a
    no-op subscription (previous hot-loop bug BLOCKER-2).
    """
    symbols = settings.PERP_SYMBOLS
    if not symbols:
        log.info("bybit_perp_no_symbols_configured")
        return
    async with session.ws_connect(
        WS_URL,
        headers=None,  # explicit: do not leak shared-session UA/auth headers
        max_msg_size=0,
    ) as ws:
        topics = [f"{_TICKER_TOPIC_PREFIX}{s}" for s in symbols]
        await ws.send_json({"op": "subscribe", "args": topics})
        try:
            ack_msg = await asyncio.wait_for(
                ws.receive_json(), timeout=_SUBSCRIBE_ACK_TIMEOUT_SEC
            )
        except asyncio.TimeoutError:
            log.warning("perp.bybit.subscribe_ack_timeout", topics=topics)
            raise
        if not ack_msg.get("success", False):
            log.error(
                "perp.bybit.subscribe_rejected",
                ret_msg=ack_msg.get("ret_msg"),
                topics=topics,
            )
            raise RuntimeError(f"bybit subscribe rejected: {ack_msg.get('ret_msg')}")
        ping_task = asyncio.create_task(_ping_loop(ws, settings))
        try:
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                try:
                    frame = json.loads(msg.data)
                except (json.JSONDecodeError, ValueError, TypeError):
                    if state is not None:
                        state.malformed_frames += 1
                    continue
                for tick in parse_frame(frame, state=state):
                    yield tick
        finally:
            ping_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ping_task


async def _ping_loop(ws: aiohttp.ClientWebSocketResponse, settings: Settings) -> None:
    while not ws.closed:
        await asyncio.sleep(settings.PERP_WS_PING_INTERVAL_SEC)
        if ws.closed:
            return
        try:
            await ws.send_json({"op": "ping"})
        except (
            ConnectionResetError,
            RuntimeError,
            aiohttp.ClientConnectionError,
        ) as exc:
            log.warning("perp.bybit.ping_failed", error=repr(exc))
            return
