"""BL-NEW-M1.5C: Minara DEX-eligibility alert extension tests."""

from __future__ import annotations

import pytest

from scout.config import Settings
from scout.trading.minara_alert import maybe_minara_command

_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "x",
    "ANTHROPIC_API_KEY": "x",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


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
        return {"platforms": {"solana": "SOLADDR"}}

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
        return {"platforms": {"solana": "SOLADDR"}}

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
        return {"platforms": {"solana": "SOLADDR"}}

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
        return {"platforms": {"solana": "SOLADDR"}}

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
        return {"platforms": {"solana": "SOLADDR"}}

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
        return {"platforms": {"solana": "SOLADDR"}}

    monkeypatch.setattr("scout.trading.minara_alert.fetch_coin_detail", _fake_detail)
    cmd = await maybe_minara_command(
        session=object(),
        settings=_settings(MINARA_ALERT_AMOUNT_USD=10.0),
        coin_id="bonk",
        amount_usd=None,
    )
    assert cmd is not None
    assert "--amount-usd 10" in cmd
