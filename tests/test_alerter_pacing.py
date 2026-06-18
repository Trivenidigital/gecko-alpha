"""TG pacing + retry_after handling + send_alert unification (P1 #2).

Imports scout.alerter (aiohttp) → runs on CI/Linux; on Windows the bare aiohttp
import hits OPENSSL_Uplink (see reference_windows_openssl_workaround).
"""

import re

import aiohttp
import pytest
import structlog
from aioresponses import aioresponses

from scout.alerter import send_alert, send_telegram_message
from scout.exceptions import AlertDeliveryError
from scout.observability import tg_pacing

URL_RE = re.compile(r"https://api\.telegram\.org/bot.*/sendMessage")


def _events(logs):
    return [e["event"] for e in logs]


async def test_429_then_retry_succeeds(settings_factory, patch_module_sleep):
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    s = settings_factory()
    with aioresponses() as mocked:
        mocked.post(
            URL_RE, status=429, payload={"ok": False, "parameters": {"retry_after": 3}}
        )
        mocked.post(URL_RE, status=200, payload={"ok": True})
        async with aiohttp.ClientSession() as sess:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message("hi", sess, s, parse_mode=None, source="t")
    ev = _events(logs)
    assert "tg_send_retry_after_429" in ev
    assert "tg_send_retry_succeeded" in ev
    assert "telegram_message_delivered" in ev


async def test_pre_send_waits_when_paced(settings_factory, patch_module_sleep):
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    s = settings_factory()
    tg_pacing.register_429(str(s.TELEGRAM_CHAT_ID), 5)  # chat already paced
    with aioresponses() as mocked:
        mocked.post(URL_RE, status=200, payload={"ok": True})
        async with aiohttp.ClientSession() as sess:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message("hi", sess, s, parse_mode=None, source="t")
    assert "tg_pacing_wait" in _events(logs)


async def test_retry_failed_records_every_429(settings_factory, patch_module_sleep):
    """Fold 3: a retry that also 429s emits a SECOND tg_dispatch_rejected_429."""
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    s = settings_factory()
    with aioresponses() as mocked:
        mocked.post(
            URL_RE, status=429, payload={"ok": False, "parameters": {"retry_after": 2}}
        )
        mocked.post(
            URL_RE, status=429, payload={"ok": False, "parameters": {"retry_after": 2}}
        )
        async with aiohttp.ClientSession() as sess:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message("hi", sess, s, parse_mode=None, source="t")
    ev = _events(logs)
    assert "tg_send_retry_failed" in ev
    assert ev.count("tg_dispatch_rejected_429") == 2


async def test_over_budget_retry_skipped(settings_factory, patch_module_sleep):
    """Fold 2: retry_after > cap → skip early retry, fall through, paced for later."""
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    s = settings_factory(TG_PACING_MAX_WAIT_SECONDS=10.0)
    with aioresponses() as mocked:
        mocked.post(
            URL_RE, status=429, payload={"ok": False, "parameters": {"retry_after": 60}}
        )
        async with aiohttp.ClientSession() as sess:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message("hi", sess, s, parse_mode=None, source="t")
    ev = _events(logs)
    assert "tg_send_retry_skipped_over_budget" in ev
    assert "tg_send_retry_after_429" not in ev
    # paced for the full 60s so the next send is pre-gated
    assert tg_pacing.pacing_wait_seconds(str(s.TELEGRAM_CHAT_ID)) > 30


async def test_pacing_disabled_no_retry(settings_factory, patch_module_sleep):
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    s = settings_factory(TG_PACING_ENABLED=False)
    with aioresponses() as mocked:
        mocked.post(
            URL_RE, status=429, payload={"ok": False, "parameters": {"retry_after": 3}}
        )
        async with aiohttp.ClientSession() as sess:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message("hi", sess, s, parse_mode=None, source="t")
    ev = _events(logs)
    assert "tg_send_retry_after_429" not in ev
    assert "tg_dispatch_rejected_429" in ev  # still recorded once


# ---- send_alert unification (Fold 1) ----


async def test_send_alert_routes_through_paced_sender(
    token_factory, settings_factory, patch_module_sleep
):
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    s = settings_factory(DISCORD_WEBHOOK_URL="")
    with aioresponses() as mocked:
        mocked.post(
            URL_RE, status=429, payload={"ok": False, "parameters": {"retry_after": 2}}
        )
        mocked.post(URL_RE, status=200, payload={"ok": True})
        async with aiohttp.ClientSession() as sess:
            with structlog.testing.capture_logs() as logs:
                await send_alert(token_factory(), ["gainers_early"], sess, s)
    delivered_sources = [
        e.get("source") for e in logs if e.get("event") == "telegram_message_delivered"
    ]
    assert "candidate_alert" in delivered_sources


async def test_send_alert_raises_alert_delivery_error_on_hard_failure(
    token_factory, settings_factory, patch_module_sleep
):
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    s = settings_factory(DISCORD_WEBHOOK_URL="")
    with aioresponses() as mocked:
        mocked.post(URL_RE, status=400, payload={"ok": False})  # hard failure, no retry
        async with aiohttp.ClientSession() as sess:
            with pytest.raises(AlertDeliveryError):
                await send_alert(token_factory(), ["x"], sess, s)


async def test_send_alert_still_attempts_discord(
    token_factory, settings_factory, patch_module_sleep
):
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    discord_url = "https://discord.example/webhook"
    s = settings_factory(DISCORD_WEBHOOK_URL=discord_url)
    with aioresponses() as mocked:
        mocked.post(URL_RE, status=200, payload={"ok": True})
        mocked.post(discord_url, status=204)
        async with aiohttp.ClientSession() as sess:
            await send_alert(token_factory(), ["gainers_early"], sess, s)
        # aioresponses records the discord call iff it was attempted
        assert any(
            str(key[1]).startswith(discord_url) for key in mocked.requests
        ), f"discord not attempted: {list(mocked.requests)}"
