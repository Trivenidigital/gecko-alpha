from __future__ import annotations

import asyncio
import json

import aiohttp
import structlog

log = structlog.get_logger("gecko.notify")


async def send_telegram(
    session: aiohttp.ClientSession, token: str, chat_id: str, text: str
) -> bool:
    """Send plain text (no parse_mode). Returns delivery success; never raises
    on transport, timeout, or malformed-response failures."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with session.post(
            url, json={"chat_id": chat_id, "text": text[:4000]}
        ) as resp:
            status = resp.status
            body = await resp.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        # Type name only: aiohttp messages can embed the URL, i.e. the bot token.
        log.warning("telegram_failed", status=None, reason=type(exc).__name__)
        return False
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning(
            "telegram_failed",
            status=status,
            reason=f"non-JSON body: {type(exc).__name__}",
        )
        return False
    if status != 200 or not isinstance(body, dict) or not body.get("ok"):
        # status and ok only: the upstream description is never logged
        ok = body.get("ok") is True if isinstance(body, dict) else None  # bool only
        log.warning("telegram_failed", status=status, ok=ok, reason="not ok")
        return False
    log.info("telegram_sent", status=status)
    return True
