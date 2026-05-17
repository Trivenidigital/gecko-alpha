"""BL-NEW-TG-BURST-PROFILE cycle 3 integration tests — send_telegram_message hook.

V15 M1 fold: uses aioresponses (project idiom — matches existing
tests/test_alerter.py:5,12,100,194 style). Real aiohttp.ClientSession,
NOT MagicMock chains for async context managers.
"""

import aiohttp
import pytest
import structlog
from aioresponses import aioresponses

from scout.alerter import send_telegram_message
from scout.config import Settings
from scout.observability.tg_dispatch_counter import reset_for_tests


def _settings(enabled: bool = True) -> Settings:
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="6337722878",  # DM (matches prod per memory)
        ANTHROPIC_API_KEY="k",
        TG_BURST_PROFILE_ENABLED=enabled,
    )


@pytest.mark.asyncio
async def test_send_telegram_message_records_dispatch_when_enabled():
    reset_for_tests()
    settings = _settings(enabled=True)
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

    with aioresponses() as m:
        m.post(url, status=200, payload={"ok": True})
        async with aiohttp.ClientSession() as session:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message(
                    "hello", session, settings,
                    parse_mode=None, source="test-suite",
                )

    observed = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert len(observed) == 1
    assert observed[0]["chat_id"] == "6337722878"
    assert observed[0]["source"] == "test-suite"


@pytest.mark.asyncio
async def test_send_telegram_message_skips_counter_when_disabled():
    reset_for_tests()
    settings = _settings(enabled=False)
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

    with aioresponses() as m:
        m.post(url, status=200, payload={"ok": True})
        async with aiohttp.ClientSession() as session:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message(
                    "hello", session, settings, parse_mode=None
                )

    observed = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert observed == []


@pytest.mark.asyncio
async def test_send_telegram_message_records_429_with_retry_after():
    """V14 fold MUST-FIX + V15 M3 fold: 429 response captures retry_after
    BEFORE body is consumed by error-path logging."""
    reset_for_tests()
    settings = _settings(enabled=True)
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

    with aioresponses() as m:
        m.post(url, status=429, payload={
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 15},
        })
        async with aiohttp.ClientSession() as session:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message(
                    "hello", session, settings,
                    parse_mode=None, source="429-test",
                )

    rejected = [e for e in logs if e.get("event") == "tg_dispatch_rejected_429"]
    assert len(rejected) == 1
    assert rejected[0]["retry_after"] == 15
    assert rejected[0]["source"] == "429-test"


@pytest.mark.asyncio
async def test_send_telegram_message_429_with_non_json_body():
    """V18 PR-review MUST-FIX #2: Telegram 429 may return non-JSON body
    (Cloudflare HTML page, plain-text outage notice, empty body). The
    json.loads() raises JSONDecodeError → caught by except → record_429
    still fires with retry_after=None. Without this test, a regression
    that removes the except clause would silently drop the rejection event.
    """
    reset_for_tests()
    settings = _settings(enabled=True)
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

    with aioresponses() as m:
        m.post(url, status=429, body=b"<html>cloudflare 429</html>",
               content_type="text/html")
        async with aiohttp.ClientSession() as session:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message(
                    "hello", session, settings,
                    parse_mode=None, source="cloudflare-429-test",
                )

    rejected = [e for e in logs if e.get("event") == "tg_dispatch_rejected_429"]
    assert len(rejected) == 1, f"non-JSON 429 should still emit rejection: {logs}"
    assert rejected[0]["retry_after"] is None
    assert rejected[0]["source"] == "cloudflare-429-test"


@pytest.mark.asyncio
async def test_send_telegram_message_429_missing_retry_after_in_json():
    """V18 PR-review MUST-FIX #3: Telegram's global-flood 429 responses
    omit parameters.retry_after per Bot API spec. .get(...) chain returns
    None — verify record_429 still fires with retry_after=None.
    """
    reset_for_tests()
    settings = _settings(enabled=True)
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

    with aioresponses() as m:
        m.post(url, status=429, payload={
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            # no "parameters" key — matches Telegram global-flood 429 shape
        })
        async with aiohttp.ClientSession() as session:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message(
                    "hello", session, settings,
                    parse_mode=None, source="global-flood-test",
                )

    rejected = [e for e in logs if e.get("event") == "tg_dispatch_rejected_429"]
    assert len(rejected) == 1
    assert rejected[0]["retry_after"] is None


@pytest.mark.asyncio
async def test_send_telegram_message_source_default_unattributed():
    """V18 PR-review SHOULD-FIX: legacy callers don't pass source=.
    Confirm default value 'unattributed' flows through to record_dispatch.
    A regression renaming the kwarg would break every legacy caller silently.
    """
    reset_for_tests()
    settings = _settings(enabled=True)
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

    with aioresponses() as m:
        m.post(url, status=200, payload={"ok": True})
        async with aiohttp.ClientSession() as session:
            with structlog.testing.capture_logs() as logs:
                # Note: no source= kwarg — legacy caller pattern
                await send_telegram_message(
                    "hello", session, settings, parse_mode=None
                )

    observed = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert len(observed) == 1
    assert observed[0]["source"] == "unattributed"


@pytest.mark.asyncio
async def test_send_telegram_message_record_dispatch_failure_is_isolated(monkeypatch):
    """V18 PR-review SHOULD-FIX: symmetric to the record_429 isolation test.
    If record_dispatch raises (e.g., singleton replaced with broken impl),
    the alerter must emit 'record_dispatch_failed' via logger.exception
    and continue with the HTTP send. Without this, the outer try/except
    would catch it as 'Telegram daily summary error' — wrong attribution.
    """
    reset_for_tests()
    settings = _settings(enabled=True)
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

    import scout.alerter as alerter_mod

    def _raise(*args, **kwargs):
        raise RuntimeError("dispatch instr broken")

    monkeypatch.setattr(alerter_mod, "record_dispatch", _raise)

    with aioresponses() as m:
        m.post(url, status=200, payload={"ok": True})
        async with aiohttp.ClientSession() as session:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message(
                    "hello", session, settings, parse_mode=None,
                )

    iso_failed = [e for e in logs if e.get("event") == "record_dispatch_failed"]
    assert len(iso_failed) == 1


@pytest.mark.asyncio
async def test_send_telegram_message_instrumentation_failure_is_isolated(monkeypatch):
    """V15 M2 fold: if record_429 raises (instrumentation regression),
    the alerter must NOT swallow it under the outer try/except — must emit
    a distinct logger.exception so operators spot instrumentation drift."""
    reset_for_tests()
    settings = _settings(enabled=True)
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

    import scout.alerter as alerter_mod

    def _raise(*args, **kwargs):
        raise RuntimeError("instr broken")

    monkeypatch.setattr(alerter_mod, "record_429", _raise)

    with aioresponses() as m:
        m.post(url, status=429, payload={
            "ok": False, "parameters": {"retry_after": 5},
        })
        async with aiohttp.ClientSession() as session:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message(
                    "hello", session, settings, parse_mode=None,
                )

    iso_failed = [e for e in logs if e.get("event") == "record_429_failed"]
    assert len(iso_failed) == 1
