"""Tests for alert delivery."""

import pytest
import aiohttp
from aioresponses import aioresponses

from scout.alerter import send_alert, format_alert_message


@pytest.fixture
def mock_aiohttp():
    with aioresponses() as m:
        yield m


def test_format_alert_message_contains_required_fields(token_factory):
    token = token_factory(
        contract_address="0xabc123", chain="solana", token_name="MoonCoin",
        ticker="MOON", token_age_days=2, market_cap_usd=75000,
        liquidity_usd=15000, volume_24h_usd=120000,
        holder_count=350, holder_growth_1h=30,
        quant_score=80, narrative_score=75, conviction_score=78,
        virality_class="High", mirofish_report="Strong viral narrative.",
    )
    signals = ["vol_liq_ratio", "holder_growth", "market_cap_range"]
    msg = format_alert_message(token, signals)

    assert "RESEARCH ONLY" in msg
    assert "MoonCoin" in msg
    assert "MOON" in msg
    assert "solana" in msg
    assert "75,000" in msg or "75000" in msg
    assert "78" in msg  # conviction score
    assert "80" in msg  # quant score
    assert "75" in msg  # narrative score
    assert "High" in msg  # virality class
    assert "vol_liq_ratio" in msg
    assert "dexscreener.com" in msg
    assert "0xabc123" in msg


def test_format_alert_message_without_narrative(token_factory):
    token = token_factory(
        contract_address="0xabc123", chain="solana", token_name="MoonCoin",
        ticker="MOON", token_age_days=2, market_cap_usd=75000,
        liquidity_usd=15000, volume_24h_usd=120000,
        holder_count=350, holder_growth_1h=30,
        quant_score=80, narrative_score=None, virality_class=None,
        mirofish_report=None, conviction_score=80,
    )
    signals = ["vol_liq_ratio"]
    msg = format_alert_message(token, signals)

    assert "RESEARCH ONLY" in msg
    assert "MoonCoin" in msg


async def test_send_alert_telegram(mock_aiohttp, token_factory, settings_factory):
    telegram_url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    mock_aiohttp.post(telegram_url, payload={"ok": True})

    token = token_factory(
        contract_address="0xabc123", chain="solana", token_name="MoonCoin",
        ticker="MOON", quant_score=80, narrative_score=75, conviction_score=78,
        virality_class="High", mirofish_report="Strong viral narrative.",
    )
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="",
    )
    signals = ["vol_liq_ratio", "holder_growth"]

    async with aiohttp.ClientSession() as session:
        await send_alert(token, signals, session, settings)


def test_alert_message_includes_momentum_flag(token_factory):
    """AC-08: Momentum flag appears in alert message when signal fired."""
    token = token_factory(
        contract_address="0xabc123", chain="solana", token_name="MoonCoin",
        ticker="MOON", quant_score=80, narrative_score=75, conviction_score=78,
        virality_class="High", mirofish_report="Strong viral narrative.",
    )
    signals = ["vol_liq_ratio", "momentum_ratio", "vol_acceleration"]
    msg = format_alert_message(token, signals)
    assert "CoinGecko Signals" in msg
    assert "1h gain accelerating" in msg.lower() or "momentum" in msg.lower()


def test_alert_message_includes_vol_spike_flag(token_factory):
    """Vol spike flag appears in alert message when signal fired."""
    token = token_factory(
        contract_address="0xabc123", chain="solana", token_name="MoonCoin",
        ticker="MOON", quant_score=80, narrative_score=75, conviction_score=78,
        virality_class="High", mirofish_report="Strong viral narrative.",
    )
    signals = ["vol_acceleration"]
    msg = format_alert_message(token, signals)
    assert "CoinGecko Signals" in msg
    assert "volume spike" in msg.lower() or "vol >>" in msg.lower()


async def test_send_alert_telegram_and_discord(mock_aiohttp, token_factory, settings_factory):
    telegram_url = "https://api.telegram.org/bottest-bot-token/sendMessage"
    discord_url = "https://discord.com/api/webhooks/test"

    mock_aiohttp.post(telegram_url, payload={"ok": True})
    mock_aiohttp.post(discord_url, payload={}, status=204)

    token = token_factory(
        contract_address="0xabc123", chain="solana", token_name="MoonCoin",
        ticker="MOON", quant_score=80, narrative_score=75, conviction_score=78,
        virality_class="High", mirofish_report="Strong viral narrative.",
    )
    settings = settings_factory(
        TELEGRAM_BOT_TOKEN="test-bot-token",
        TELEGRAM_CHAT_ID="test-chat-id",
        DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/test",
    )
    signals = ["vol_liq_ratio"]

    async with aiohttp.ClientSession() as session:
        await send_alert(token, signals, session, settings)
