import aiohttp
from aioresponses import aioresponses

from gecko.notify import send_telegram

URL = "https://api.telegram.org/bottok/sendMessage"


async def test_send_telegram_ok():
    with aioresponses() as m:
        m.post(URL, payload={"ok": True})
        async with aiohttp.ClientSession() as session:
            assert await send_telegram(session, "tok", "1", "hi") is True


async def test_send_telegram_ok_false():
    with aioresponses() as m:
        m.post(URL, payload={"ok": False, "description": "chat not found"})
        async with aiohttp.ClientSession() as session:
            assert await send_telegram(session, "tok", "1", "hi") is False


async def test_send_telegram_html_502_returns_false():
    with aioresponses() as m:
        m.post(
            URL,
            status=502,
            body="<html>Bad Gateway</html>",
            content_type="text/html",
        )
        async with aiohttp.ClientSession() as session:
            assert await send_telegram(session, "tok", "1", "hi") is False


async def test_send_telegram_connection_error_returns_false():
    with aioresponses() as m:
        m.post(URL, exception=aiohttp.ClientConnectionError())
        async with aiohttp.ClientSession() as session:
            assert await send_telegram(session, "tok", "1", "hi") is False
