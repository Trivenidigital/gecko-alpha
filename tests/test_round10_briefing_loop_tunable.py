"""Round 10: BRIEFING_LOOP_POLL_INTERVAL_SEC bounds + use-site assertion.

Replaces the hardcoded `await asyncio.sleep(60)` at scout/main.py:561 with
`await asyncio.sleep(settings.BRIEFING_LOOP_POLL_INTERVAL_SEC)`. Operators
can now tune the poll cadence via .env without code change. Bounds:
ge=10 (sub-10s thrashes a sleep-heavy loop), le=3600 (beyond 1h the
trigger window may close before the next poll fires).
"""

from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from scout.config import Settings
from scout import main as scout_main


@pytest.fixture
def _min_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "x")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "x")


def test_briefing_loop_poll_interval_default(_min_env):
    s = Settings()
    assert s.BRIEFING_LOOP_POLL_INTERVAL_SEC == 60


def test_briefing_loop_poll_interval_too_small_rejected(_min_env, monkeypatch):
    monkeypatch.setenv("BRIEFING_LOOP_POLL_INTERVAL_SEC", "5")
    with pytest.raises(ValidationError, match="BRIEFING_LOOP_POLL_INTERVAL_SEC"):
        Settings()


def test_briefing_loop_poll_interval_too_large_rejected(_min_env, monkeypatch):
    monkeypatch.setenv("BRIEFING_LOOP_POLL_INTERVAL_SEC", "7200")
    with pytest.raises(ValidationError, match="BRIEFING_LOOP_POLL_INTERVAL_SEC"):
        Settings()


def test_briefing_loop_poll_interval_bounds_admitted(_min_env, monkeypatch):
    for value in ("10", "60", "300", "3600"):
        monkeypatch.setenv("BRIEFING_LOOP_POLL_INTERVAL_SEC", value)
        s = Settings()
        assert s.BRIEFING_LOOP_POLL_INTERVAL_SEC == int(value)


def test_briefing_loop_uses_settings_not_hardcoded():
    """Static guard: scout/main.py:briefing_loop must read sleep cadence
    from settings, not a hardcoded literal."""
    src = inspect.getsource(scout_main.briefing_loop)
    # Hardcoded literal must be gone.
    assert "asyncio.sleep(60)" not in src, (
        "scout/main.py briefing_loop reintroduced hardcoded asyncio.sleep(60). "
        "Use settings.BRIEFING_LOOP_POLL_INTERVAL_SEC so operators can tune "
        "without a code change."
    )
    # Settings reference must be present.
    assert "BRIEFING_LOOP_POLL_INTERVAL_SEC" in src, (
        "scout/main.py briefing_loop should sleep via "
        "settings.BRIEFING_LOOP_POLL_INTERVAL_SEC"
    )
