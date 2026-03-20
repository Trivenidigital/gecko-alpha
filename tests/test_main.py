"""Tests for main pipeline loop."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from scout.main import run_cycle


@pytest.fixture
def mock_settings():
    with patch("scout.main.Settings") as MockSettings:
        settings = MagicMock()
        settings.SCAN_INTERVAL_SECONDS = 60
        settings.MIN_SCORE = 60
        settings.DB_PATH = ":memory:"
        MockSettings.return_value = settings
        yield settings


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.initialize = AsyncMock()
    db.close = AsyncMock()
    db.upsert_candidate = AsyncMock()
    db.log_alert = AsyncMock()
    db.get_daily_mirofish_count = AsyncMock(return_value=0)
    db.get_daily_alert_count = AsyncMock(return_value=0)
    return db


@pytest.fixture
def mock_session():
    return AsyncMock()


async def test_run_cycle_dry_run(mock_db, mock_session, mock_settings):
    """Dry-run mode: pipeline runs but no alerts are sent."""
    from scout.models import CandidateToken

    token = CandidateToken(
        contract_address="0xtest", chain="solana", token_name="Test",
        ticker="TST", token_age_days=1, market_cap_usd=50000,
        liquidity_usd=10000, volume_24h_usd=80000,
        holder_count=100, holder_growth_1h=25,
    )

    with patch("scout.main.fetch_trending", new_callable=AsyncMock, return_value=[token]), \
         patch("scout.main.fetch_trending_pools", new_callable=AsyncMock, return_value=[]), \
         patch("scout.main.cg_fetch_top_movers", new_callable=AsyncMock, return_value=[]), \
         patch("scout.main.cg_fetch_trending", new_callable=AsyncMock, return_value=[]), \
         patch("scout.main.enrich_holders", new_callable=AsyncMock, side_effect=lambda t, s, st: t), \
         patch("scout.main.aggregate", return_value=[token]), \
         patch("scout.main.score", return_value=(75, ["vol_liq_ratio", "holder_growth"])), \
         patch("scout.main.evaluate", new_callable=AsyncMock, return_value=(True, 78.0, token)), \
         patch("scout.main.is_safe", new_callable=AsyncMock, return_value=True), \
         patch("scout.main.send_alert", new_callable=AsyncMock) as mock_alert:

        stats = await run_cycle(mock_settings, mock_db, mock_session, dry_run=True)

    # In dry-run, alerts should NOT be sent
    mock_alert.assert_not_called()
    assert stats["tokens_scanned"] >= 1


async def test_run_cycle_sends_alert(mock_db, mock_session, mock_settings):
    """Normal mode: alert fires when token passes all gates."""
    from scout.models import CandidateToken

    token = CandidateToken(
        contract_address="0xtest", chain="solana", token_name="Test",
        ticker="TST", token_age_days=1, market_cap_usd=50000,
        liquidity_usd=10000, volume_24h_usd=80000,
        holder_count=100, holder_growth_1h=25,
    )

    with patch("scout.main.fetch_trending", new_callable=AsyncMock, return_value=[token]), \
         patch("scout.main.fetch_trending_pools", new_callable=AsyncMock, return_value=[]), \
         patch("scout.main.cg_fetch_top_movers", new_callable=AsyncMock, return_value=[]), \
         patch("scout.main.cg_fetch_trending", new_callable=AsyncMock, return_value=[]), \
         patch("scout.main.enrich_holders", new_callable=AsyncMock, side_effect=lambda t, s, st: t), \
         patch("scout.main.aggregate", return_value=[token]), \
         patch("scout.main.score", return_value=(75, ["vol_liq_ratio"])), \
         patch("scout.main.evaluate", new_callable=AsyncMock, return_value=(True, 78.0, token)), \
         patch("scout.main.is_safe", new_callable=AsyncMock, return_value=True), \
         patch("scout.main.send_alert", new_callable=AsyncMock) as mock_alert:

        stats = await run_cycle(mock_settings, mock_db, mock_session, dry_run=False)

    mock_alert.assert_called_once()
    assert stats["alerts_fired"] == 1


async def test_run_cycle_skips_unsafe_token(mock_db, mock_session, mock_settings):
    """Unsafe token (GoPlus check fails) -> no alert."""
    from scout.models import CandidateToken

    token = CandidateToken(
        contract_address="0xrug", chain="solana", token_name="Rug",
        ticker="RUG", token_age_days=1, market_cap_usd=50000,
        liquidity_usd=10000, volume_24h_usd=80000,
        holder_count=100, holder_growth_1h=25,
    )

    with patch("scout.main.fetch_trending", new_callable=AsyncMock, return_value=[token]), \
         patch("scout.main.fetch_trending_pools", new_callable=AsyncMock, return_value=[]), \
         patch("scout.main.cg_fetch_top_movers", new_callable=AsyncMock, return_value=[]), \
         patch("scout.main.cg_fetch_trending", new_callable=AsyncMock, return_value=[]), \
         patch("scout.main.enrich_holders", new_callable=AsyncMock, side_effect=lambda t, s, st: t), \
         patch("scout.main.aggregate", return_value=[token]), \
         patch("scout.main.score", return_value=(75, ["vol_liq_ratio"])), \
         patch("scout.main.evaluate", new_callable=AsyncMock, return_value=(True, 78.0, token)), \
         patch("scout.main.is_safe", new_callable=AsyncMock, return_value=False), \
         patch("scout.main.send_alert", new_callable=AsyncMock) as mock_alert:

        stats = await run_cycle(mock_settings, mock_db, mock_session, dry_run=False)

    mock_alert.assert_not_called()
