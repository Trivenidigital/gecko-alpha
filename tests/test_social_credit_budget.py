"""Tests for scout.social.lunarcrush.credits -- persistent credit ledger."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.social.lunarcrush.credits import CreditLedger, flush_credit_ledger


def _settings(**overrides) -> Settings:
    defaults = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        LUNARCRUSH_API_KEY="k",
        LUNARCRUSH_ENABLED=True,
        LUNARCRUSH_DAILY_CREDIT_BUDGET=2000,
        LUNARCRUSH_CREDIT_SOFT_PCT=0.80,
        LUNARCRUSH_CREDIT_HARD_PCT=0.95,
        LUNARCRUSH_POLL_INTERVAL=300,
        LUNARCRUSH_POLL_INTERVAL_SOFT=600,
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "credit.db")
    await d.initialize()
    yield d
    await d.close()


def test_initial_state_normal():
    s = _settings()
    ledger = CreditLedger(s)
    assert ledger.current_poll_interval() == 300
    assert ledger.is_exhausted() is False


def test_soft_downshift_at_80_percent():
    """At 80% of budget the poll interval downshifts to the soft value."""
    s = _settings(LUNARCRUSH_DAILY_CREDIT_BUDGET=2000, LUNARCRUSH_CREDIT_SOFT_PCT=0.80)
    ledger = CreditLedger(s)
    ledger.consume(1_600)  # 80%
    assert ledger.current_poll_interval() == 600


def test_hard_stop_at_95_percent():
    """At 95% the detector is marked exhausted; cycles skip the fetch."""
    s = _settings(LUNARCRUSH_DAILY_CREDIT_BUDGET=2000)
    ledger = CreditLedger(s)
    ledger.consume(1_900)  # 95%
    assert ledger.is_exhausted() is True


def test_midnight_utc_rollover_resets_counter():
    """At midnight UTC (fake clock) the counter resets back to 0."""
    s = _settings()
    day = 18
    current = [datetime(2026, 4, day, 23, 0, tzinfo=timezone.utc)]

    def clock():
        return current[0]

    ledger = CreditLedger(s, clock=clock)
    ledger.consume(1_900)
    assert ledger.is_exhausted() is True

    # Advance one hour past midnight
    current[0] = datetime(2026, 4, day + 1, 0, 1, tzinfo=timezone.utc)
    # Rollover triggers on any public accessor call.
    ledger.maybe_rollover()
    assert ledger.credits_used == 0
    assert ledger.is_exhausted() is False


@pytest.mark.asyncio
async def test_ledger_persists_across_restart(db):
    """Flushing, closing, restoring the DB preserves credit state."""
    s = _settings()
    ledger = CreditLedger(s)
    ledger.consume(500)
    await flush_credit_ledger(db, ledger)

    # Reinstantiate as if process restarted
    ledger2 = CreditLedger(s)
    await ledger2.hydrate(db)
    assert ledger2.credits_used == 500


def test_soft_to_hard_transition_midcycle():
    """Consuming from 94% straight into 96% flips to exhausted mid-cycle."""
    s = _settings()
    ledger = CreditLedger(s)
    ledger.consume(1_880)  # 94%
    assert ledger.is_exhausted() is False
    ledger.consume(40)  # now 96%
    assert ledger.is_exhausted() is True
