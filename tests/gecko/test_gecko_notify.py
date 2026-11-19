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
    from structlog.testing import capture_logs

    with aioresponses() as m:
        m.post(URL, status=400, payload={"ok": False, "description": "chat not found"})
        async with aiohttp.ClientSession() as session:
            with capture_logs() as logs:
                assert await send_telegram(session, "tok", "1", "hi") is False
    (e,) = [e for e in logs if e["event"] == "telegram_failed"]
    assert (e["status"], e["ok"]) == (400, False)
    assert "chat not found" not in repr(logs)  # upstream text is never logged


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


async def test_send_telegram_failure_log_never_carries_the_token():
    from structlog.testing import capture_logs

    secret = "123456:SECRET-bot-token"
    url = f"https://api.telegram.org/bot{secret}/sendMessage"
    with aioresponses() as m:
        m.post(url, exception=aiohttp.ClientConnectionError(f"cannot connect {url}"))
        async with aiohttp.ClientSession() as session:
            with capture_logs() as logs:
                assert await send_telegram(session, secret, "1", "hi") is False
    (e,) = [e for e in logs if e["event"] == "telegram_failed"]
    assert e["reason"] == "ClientConnectionError"
    assert "SECRET" not in repr(logs)
