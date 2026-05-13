"""BL-NEW-M1.5C: Minara DEX-eligibility alert extension tests."""

from __future__ import annotations

import asyncio

import pytest
from structlog.testing import capture_logs

from scout.config import Settings
from scout.trading.minara_alert import (
    maybe_minara_command,
    persist_minara_alert_emission,
)

_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "x",
    "ANTHROPIC_API_KEY": "x",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


class _FakeEmissionDb:
    def __init__(self):
        self.calls = []

    async def record_minara_alert_emission(self, **kwargs):
        self.calls.append(kwargs)
        return True


class _FailingEmissionDb:
    async def record_minara_alert_emission(self, **kwargs):
        raise RuntimeError("db nope")


class _TimeoutEmissionDb:
    async def record_minara_alert_emission(self, **kwargs):
        raise asyncio.TimeoutError()


@pytest.mark.asyncio
async def test_returns_command_for_solana_token(monkeypatch):
    """Token with platforms.solana set → formatted command returned."""

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"solana": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    settings = _settings(MINARA_ALERT_FROM_TOKEN="USDC")
    cmd = await maybe_minara_command(
        session=object(),
        settings=settings,
        coin_id="bonk",
        amount_usd=10.0,
    )
    assert cmd is not None
    assert "minara swap" in cmd
    assert "USDC" in cmd
    assert "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" in cmd
    assert "--amount-usd 10" in cmd


@pytest.mark.asyncio
async def test_persists_emission_when_db_context_supplied(monkeypatch):
    fake_db = _FakeEmissionDb()
    cmd = "minara swap --from USDC --to DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263 --amount-usd 10"
    await persist_minara_alert_emission(
        coin_id="bonk",
        db=fake_db,
        paper_trade_id=42,
        signal_type="gainers_early",
        tg_alert_log_id=99,
        chain="solana",
        amount_usd=10,
        command_text=cmd,
    )
    assert fake_db.calls == [
        {
            "paper_trade_id": 42,
            "tg_alert_log_id": 99,
            "signal_type": "gainers_early",
            "coin_id": "bonk",
            "chain": "solana",
            "amount_usd": 10,
            "command_text": cmd,
            "source_event_id": "tg_alert_log:99",
            "lock_timeout_sec": 0.25,
        }
    ]


@pytest.mark.asyncio
async def test_persistence_failure_returns_command_and_logs(monkeypatch):
    with capture_logs() as logs:
        await persist_minara_alert_emission(
            coin_id="bonk",
            db=_FailingEmissionDb(),
            paper_trade_id=42,
            signal_type="gainers_early",
            tg_alert_log_id=99,
            chain="solana",
            amount_usd=10,
            command_text="minara swap --from USDC --to ABC --amount-usd 10",
        )
    assert any(e["event"] == "minara_alert_emission_persist_failed" for e in logs)


@pytest.mark.asyncio
async def test_persistence_timeout_returns_command_and_logs(monkeypatch):
    with capture_logs() as logs:
        await persist_minara_alert_emission(
            coin_id="bonk",
            db=_TimeoutEmissionDb(),
            paper_trade_id=42,
            signal_type="gainers_early",
            tg_alert_log_id=99,
            chain="solana",
            amount_usd=10,
            command_text="minara swap --from USDC --to ABC --amount-usd 10",
        )
    assert any(e["event"] == "minara_alert_emission_persist_timeout" for e in logs)


@pytest.mark.asyncio
async def test_returns_none_when_no_solana_platform(monkeypatch):
    """Token without platforms.solana → None."""

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"ethereum": "0xabc"}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(),
        coin_id="random",
        amount_usd=10.0,
    )
    assert cmd is None


@pytest.mark.asyncio
async def test_returns_none_when_solana_platform_empty(monkeypatch):
    """Empty SPL address → None."""

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"solana": ""}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(),
        coin_id="solana",
        amount_usd=10.0,
    )
    assert cmd is None


@pytest.mark.asyncio
async def test_returns_none_when_fetch_detail_fails(monkeypatch):
    """CG 404 / 429 / network error → None (never raises)."""

    async def _fake_detail(session, coin_id, api_key=""):
        return None

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(),
        coin_id="missing",
        amount_usd=10.0,
    )
    assert cmd is None


@pytest.mark.asyncio
async def test_returns_none_when_disabled(monkeypatch):
    """MINARA_ALERT_ENABLED=False → no fetch, immediate None."""
    fetch_count = [0]

    async def _fake_detail(*args, **kwargs):
        fetch_count[0] += 1
        return {"platforms": {"solana": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(MINARA_ALERT_ENABLED=False),
        coin_id="bonk",
        amount_usd=10.0,
    )
    assert cmd is None
    assert fetch_count[0] == 0, "should short-circuit before fetch"


@pytest.mark.asyncio
async def test_handles_unexpected_exception(monkeypatch):
    """fetch_coin_detail raising unexpectedly → None."""

    async def _fake_detail_raise(*args, **kwargs):
        raise RuntimeError("simulated CG outage")

    monkeypatch.setattr(
        "scout.trading.minara_alert.fetch_coin_detail", _fake_detail_raise
    )
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(),
        coin_id="bonk",
        amount_usd=10.0,
    )
    assert cmd is None


@pytest.mark.asyncio
async def test_uses_settings_amount_not_caller(monkeypatch):
    """R2-C1 fold: command size uses MINARA_ALERT_AMOUNT_USD, NOT caller's amount."""

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"solana": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(MINARA_ALERT_AMOUNT_USD=5.0),
        coin_id="bonk",
        amount_usd=300.0,
    )
    assert cmd is not None
    assert "--amount-usd 5" in cmd
    assert "300" not in cmd


@pytest.mark.asyncio
async def test_default_amount_is_10_dollars(monkeypatch):
    """R2-C1 fold: default MINARA_ALERT_AMOUNT_USD=10."""

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"solana": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(),
        coin_id="bonk",
        amount_usd=999.0,
    )
    assert "--amount-usd 10" in cmd


@pytest.mark.asyncio
async def test_returns_none_when_session_is_none(monkeypatch):
    """R1-I1 fold: session=None short-circuits before fetch."""
    fetch_count = [0]

    async def _fake_detail(*args, **kwargs):
        fetch_count[0] += 1
        return {"platforms": {"solana": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=None,
        settings=_settings(),
        coin_id="bonk",
        amount_usd=10.0,
    )
    assert cmd is None
    assert fetch_count[0] == 0


@pytest.mark.asyncio
async def test_amount_clamps_to_minimum_1_dollar(monkeypatch):
    """R1-I2 fold: emit --amount-usd ≥ 1 even if Settings has tiny value."""

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"solana": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(MINARA_ALERT_AMOUNT_USD=0.4),
        coin_id="bonk",
        amount_usd=10.0,
    )
    assert cmd is not None
    assert "--amount-usd 1" in cmd
    assert "--amount-usd 0" not in cmd


@pytest.mark.asyncio
async def test_amount_handles_none_gracefully(monkeypatch):
    """R1-I3 fold: amount_usd=None doesn't crash; size from Settings."""

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"solana": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(MINARA_ALERT_AMOUNT_USD=10.0),
        coin_id="bonk",
        amount_usd=None,
    )
    assert cmd is not None
    assert "--amount-usd 10" in cmd


@pytest.mark.asyncio
async def test_returns_none_when_platforms_is_not_dict(monkeypatch):
    """PR-V1-I1 fold: CG schema drift (platforms=string/list) → None,
    no spurious format_failed log."""

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": "oops"}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(),
        coin_id="bonk",
        amount_usd=10.0,
    )
    assert cmd is None


@pytest.mark.asyncio
async def test_returns_none_when_platforms_is_null(monkeypatch):
    """Bitcoin literal: `{"platforms": null}` → None via `or {}` fallback."""

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": None}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(),
        coin_id="bitcoin",
        amount_usd=10.0,
    )
    assert cmd is None


@pytest.mark.asyncio
async def test_rejects_evm_shaped_address_under_solana_key(monkeypatch):
    """PR-V1-I1 + V2-I2 fold: corrupt CG data putting an EVM hex address
    (`0xabc...`, contains '0' which is not in base58 alphabet) under the
    solana platforms key must NOT emit a malformed Run: line."""

    async def _fake_detail(session, coin_id, api_key=""):
        return {
            "platforms": {
                "solana": "0xabcdef0123456789abcdef0123456789abcdef01",
            }
        }

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(),
        coin_id="corrupted",
        amount_usd=10.0,
    )
    assert cmd is None, "corrupt EVM address must be rejected by shape check"


@pytest.mark.asyncio
async def test_rejects_address_too_short(monkeypatch):
    """SPL addresses are 32-44 chars; reject shorter."""

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"solana": "abc"}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(),
        coin_id="tiny",
        amount_usd=10.0,
    )
    assert cmd is None


@pytest.mark.asyncio
async def test_accepts_real_world_spl_address(monkeypatch):
    """Sanity: a real BONK-like SPL address passes the shape check."""

    async def _fake_detail(session, coin_id, api_key=""):
        return {"platforms": {"solana": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263"}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(),
        coin_id="bonk",
        amount_usd=10.0,
    )
    assert cmd is not None
    assert "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263" in cmd
