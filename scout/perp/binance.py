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
    from scout.perp.watcher import ClassifierState

log = structlog.get_logger(__name__)

# Stream name constants
_MARK_STREAM = "!markPrice@arr@1s"
_OI_STREAM_SUFFIX = "@openInterest"
_MARK_STREAM_MATCH = "markPrice@arr"  # matches "!markPrice@arr@1s" frames
_OI_STREAM_MATCH = "openInterest"


def parse_frame(
    frame: dict[str, Any],
    state: "ClassifierState | None" = None,
) -> list[PerpTick]:
    """Yield PerpTicks from a single Binance WS frame.

    Supports:
      * ``!markPrice@arr@1s`` — array of markPrice updates.
      * ``<symbol>@openInterest`` — single OI update.

    Malformed or unknown streams silently yield empty. Never raises.
    Per-item parse failures increment state.parse_rejects if state is provided.
    """
    ticks: list[PerpTick] = []
    stream = frame.get("stream") if isinstance(frame, dict) else None
    if stream and _MARK_STREAM_MATCH in stream:
        data = frame.get("data")
        if isinstance(data, list):
            for item in data:
                tick = _parse_mark(item)
                if tick is not None:
                    ticks.append(tick)
                elif state is not None:
                    state.parse_rejects += 1
    elif stream and _OI_STREAM_MATCH in stream:
        data = frame.get("data")
        if isinstance(data, dict):
            tick = _parse_oi(data)
            if tick is not None:
                ticks.append(tick)
            elif state is not None:
                state.parse_rejects += 1
    # OI and markPrice frames are snapshots of current value, not deltas;
    # drop-oldest in the queue is safe for both stream types.
    return ticks


def _parse_mark(item: dict[str, Any]) -> PerpTick | None:
    try:
        symbol = str(item.get("s", ""))
        ticker = normalize_ticker(symbol)
        if ticker is None:
            return None
        raw_p = item.get("p")
        raw_r = item.get("r")
        return PerpTick(
            exchange="binance",
            symbol=symbol,
            ticker=ticker,
            mark_price=(float(raw_p) if raw_p is not None else None),
            funding_rate=(float(raw_r) if raw_r is not None else None),
            timestamp=datetime.fromtimestamp(
                float(item.get("E", 0)) / 1000, tz=timezone.utc
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("perp.binance.parse_failed", kind="markPrice", error=repr(exc))
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
    except (KeyError, TypeError, ValueError) as exc:
        log.warning("perp.binance.parse_failed", kind="openInterest", error=repr(exc))
        return None


async def stream_ticks(
    session: aiohttp.ClientSession,
    settings: Settings,
    state: "ClassifierState | None" = None,
) -> AsyncIterator[PerpTick]:  # type: ignore[return]
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
        [_MARK_STREAM] + [f"{s.lower()}{_OI_STREAM_SUFFIX}" for s in symbols]
    )
    url = f"{settings.PERP_BINANCE_WS_URL}?streams={streams}"
    async with session.ws_connect(
        url,
        headers=None,  # explicit: do not leak shared-session UA/auth headers
        max_msg_size=0,
        # No heartbeat kwarg: Binance server sends pings unsolicited and
        # aiohttp auto-pongs on receipt. Client heartbeat is redundant.
    ) as ws:
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
