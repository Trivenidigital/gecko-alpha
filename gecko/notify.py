from __future__ import annotations

import aiohttp
import structlog

log = structlog.get_logger("gecko.notify")


async def send_telegram(
    session: aiohttp.ClientSession, token: str, chat_id: str, text: str
) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with session.post(
        url, json={"chat_id": chat_id, "text": text[:4000]}
    ) as resp:
        body = await resp.json(content_type=None)
        ok = bool(body.get("ok")) and resp.status == 200
        log.info("telegram_sent" if ok else "telegram_failed", status=resp.status)
        return ok
