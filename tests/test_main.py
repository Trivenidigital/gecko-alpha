"""Tests for main pipeline loop."""

from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from scout.main import _fetch_coingecko_lanes, run_cycle


@pytest.fixture
def mock_settings():
    with patch("scout.main.Settings") as MockSettings:
        settings = MagicMock()
        settings.SCAN_INTERVAL_SECONDS = 60
        settings.MIN_SCORE = 60
        settings.DB_PATH = ":memory:"
        settings.PERP_ENABLED = False
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
    db.get_previous_holder_count = AsyncMock(return_value=None)
    db.log_holder_snapshot = AsyncMock()
    db.log_score = AsyncMock()
    db.get_recent_scores = AsyncMock(return_value=[])
    db.get_vol_7d_avg = AsyncMock(return_value=None)
    db.log_volume_snapshot = AsyncMock()
    db.was_recently_alerted = AsyncMock(return_value=False)
    return db


@pytest.fixture
def mock_session():
    return AsyncMock()


def test_combine_coin_market_rows_includes_trending_and_dedupes():
    """Raw-market fan-in includes hydrated trending rows for signal surfaces."""
    from scout.main import _combine_coin_market_rows

    top_movers = [{"id": "alpha", "source": "top"}]
    trending = [{"id": "beta", "source": "trending"}, {"id": "alpha", "source": "late"}]
    by_volume = [{"id": "gamma", "source": "volume"}]

    combined = _combine_coin_market_rows(top_movers, trending, by_volume)

    assert [row["id"] for row in combined] == ["alpha", "beta", "gamma"]
    assert combined[0]["source"] == "top"


class _FakeLimiter:
    def __init__(self):
        self.backing_off = False

    def is_backing_off(self):
        return self.backing_off


async def test_fetch_coingecko_lanes_runs_held_position_first(monkeypatch, mock_db):
    """Lane order invariant: held_position is FIRST so its 1 /simple/price call
    is not starved by the 7-10 calls/cycle scanner lanes consume.

    See `tasks/findings_cg_budget_attribution_2026_05_18.md`.
    """
    calls = []
    limiter = _FakeLimiter()

    async def _held_position(session, settings, db):
        calls.append("held_position_prices")
        return [{"id": "held-1", "current_price": 1.0}]

    async def _top_movers(session, settings):
        calls.append("top_movers")
        return []

    async def _trending(session, settings):
        calls.append("trending")
        return []

    async def _by_volume(session, settings):
        calls.append("by_volume")
        return []

    async def _midcap(session, settings):
        calls.append("midcap_gainers")
        return []

    monkeypatch.setattr("scout.main.coingecko_limiter", limiter)
    monkeypatch.setattr("scout.main.fetch_held_position_prices", _held_position)
    monkeypatch.setattr("scout.main.cg_fetch_top_movers", _top_movers)
    monkeypatch.setattr("scout.main.cg_fetch_trending", _trending)
    monkeypatch.setattr("scout.main.cg_fetch_by_volume", _by_volume)
    monkeypatch.setattr("scout.main.cg_fetch_midcap_gainers", _midcap)

    cg_movers, cg_trending, cg_by_volume, cg_midcap, held = (
        await _fetch_coingecko_lanes(AsyncMock(), MagicMock(), mock_db)
    )

    # Lane order: held_position must be the first lane invoked.
    assert calls[0] == "held_position_prices"
    # All five lanes run when no backoff.
    assert calls == [
        "held_position_prices",
        "top_movers",
        "trending",
        "by_volume",
        "midcap_gainers",
    ]
    # Held result preserved.
    assert held == [{"id": "held-1", "current_price": 1.0}]


async def test_fetch_coingecko_lanes_stops_scanners_when_held_position_triggers_backoff(
    monkeypatch, mock_db
):
    """If held_position itself trips the shared limiter into backoff, scanner
    lanes are skipped but the held_position_raw payload is preserved in
    tuple position 4."""
    calls = []
    limiter = _FakeLimiter()

    async def _held_position(session, settings, db):
        calls.append("held_position_prices")
        limiter.backing_off = True
        return [{"id": "held-1"}]

    async def _unexpected(*args):
        calls.append("unexpected")
        return []

    monkeypatch.setattr("scout.main.coingecko_limiter", limiter)
    monkeypatch.setattr("scout.main.fetch_held_position_prices", _held_position)
    monkeypatch.setattr("scout.main.cg_fetch_top_movers", _unexpected)
    monkeypatch.setattr("scout.main.cg_fetch_trending", _unexpected)
    monkeypatch.setattr("scout.main.cg_fetch_by_volume", _unexpected)
    monkeypatch.setattr("scout.main.cg_fetch_midcap_gainers", _unexpected)

    result = await _fetch_coingecko_lanes(AsyncMock(), MagicMock(), mock_db)

    assert result == ([], [], [], [], [{"id": "held-1"}])
    assert calls == ["held_position_prices"]


async def test_fetch_coingecko_lanes_preserves_held_when_scanner_triggers_backoff(
    monkeypatch, mock_db
):
    """The lane-reorder fix's intent: scanner-triggered backoff AFTER
    held_position must not lose the held_position payload. Under the prior
    ordering (held last), this scenario produced 0 refreshed."""
    calls = []
    limiter = _FakeLimiter()

    async def _held_position(session, settings, db):
        calls.append("held_position_prices")
        return [{"id": "held-x"}]

    async def _top_movers(session, settings):
        calls.append("top_movers")
        # Simulate scanner triggering 429 cooldown — under the prior order
        # this would have starved held_position; under the new order it
        # only stops the remaining scanner cascade.
        limiter.backing_off = True
        return []

    async def _unexpected(*args):
        calls.append("unexpected")
        return []

    monkeypatch.setattr("scout.main.coingecko_limiter", limiter)
    monkeypatch.setattr("scout.main.fetch_held_position_prices", _held_position)
    monkeypatch.setattr("scout.main.cg_fetch_top_movers", _top_movers)
    monkeypatch.setattr("scout.main.cg_fetch_trending", _unexpected)
    monkeypatch.setattr("scout.main.cg_fetch_by_volume", _unexpected)
    monkeypatch.setattr("scout.main.cg_fetch_midcap_gainers", _unexpected)

    cg_movers, cg_trending, cg_by_volume, cg_midcap, held = (
        await _fetch_coingecko_lanes(AsyncMock(), MagicMock(), mock_db)
    )

    # Held data preserved even after scanner triggered backoff.
    assert held == [{"id": "held-x"}]
    # Remaining scanners after top_movers were skipped per backoff cascade.
    assert calls == ["held_position_prices", "top_movers"]


async def test_run_cycle_dry_run(mock_db, mock_session, mock_settings):
    """Dry-run mode: pipeline runs but no alerts are sent."""
    from scout.models import CandidateToken

    token = CandidateToken(
        contract_address="0xtest",
        chain="solana",
        token_name="Test",
        ticker="TST",
        token_age_days=1,
        market_cap_usd=50000,
        liquidity_usd=10000,
        volume_24h_usd=80000,
        holder_count=100,
        holder_growth_1h=25,
    )

    with (
        patch(
            "scout.main.fetch_trending", new_callable=AsyncMock, return_value=[token]
        ),
        patch(
            "scout.main.fetch_trending_pools", new_callable=AsyncMock, return_value=[]
        ),
        patch(
            "scout.main.cg_fetch_top_movers", new_callable=AsyncMock, return_value=[]
        ),
        patch("scout.main.cg_fetch_trending", new_callable=AsyncMock, return_value=[]),
        patch(
            "scout.main.enrich_holders",
            new_callable=AsyncMock,
            side_effect=lambda t, s, st: t,
        ),
        patch("scout.main.aggregate", return_value=[token]),
        patch(
            "scout.main.score", return_value=(75, ["vol_liq_ratio", "holder_growth"])
        ),
        patch(
            "scout.main.evaluate",
            new_callable=AsyncMock,
            return_value=(True, 78.0, token),
        ),
        patch("scout.main.is_safe", new_callable=AsyncMock, return_value=True),
        patch("scout.main.send_alert", new_callable=AsyncMock) as mock_alert,
    ):

        stats = await run_cycle(mock_settings, mock_db, mock_session, dry_run=True)

    # In dry-run, alerts should NOT be sent
    mock_alert.assert_not_called()
    assert stats["tokens_scanned"] >= 1


async def test_run_cycle_sends_alert(mock_db, mock_session, mock_settings):
    """Normal mode: alert fires when token passes all gates."""
    from scout.models import CandidateToken

    token = CandidateToken(
        contract_address="0xtest",
        chain="solana",
        token_name="Test",
        ticker="TST",
        token_age_days=1,
        market_cap_usd=50000,
        liquidity_usd=10000,
        volume_24h_usd=80000,
        holder_count=100,
        holder_growth_1h=25,
    )

    with (
        patch(
            "scout.main.fetch_trending", new_callable=AsyncMock, return_value=[token]
        ),
        patch(
            "scout.main.fetch_trending_pools", new_callable=AsyncMock, return_value=[]
        ),
        patch(
            "scout.main.cg_fetch_top_movers", new_callable=AsyncMock, return_value=[]
        ),
        patch("scout.main.cg_fetch_trending", new_callable=AsyncMock, return_value=[]),
        patch(
            "scout.main.enrich_holders",
            new_callable=AsyncMock,
            side_effect=lambda t, s, st: t,
        ),
        patch("scout.main.aggregate", return_value=[token]),
        patch("scout.main.score", return_value=(75, ["vol_liq_ratio"])),
        patch(
            "scout.main.evaluate",
            new_callable=AsyncMock,
            return_value=(True, 78.0, token),
        ),
        patch("scout.main.is_safe", new_callable=AsyncMock, return_value=True),
        patch("scout.main.send_alert", new_callable=AsyncMock) as mock_alert,
    ):

        stats = await run_cycle(mock_settings, mock_db, mock_session, dry_run=False)

    mock_alert.assert_called_once()
    assert stats["alerts_fired"] == 1


async def test_run_cycle_skips_unsafe_token(mock_db, mock_session, mock_settings):
    """Unsafe token (GoPlus check fails) -> no alert."""
    from scout.models import CandidateToken

    token = CandidateToken(
        contract_address="0xrug",
        chain="solana",
        token_name="Rug",
        ticker="RUG",
        token_age_days=1,
        market_cap_usd=50000,
        liquidity_usd=10000,
        volume_24h_usd=80000,
        holder_count=100,
        holder_growth_1h=25,
    )

    with (
        patch(
            "scout.main.fetch_trending", new_callable=AsyncMock, return_value=[token]
        ),
        patch(
            "scout.main.fetch_trending_pools", new_callable=AsyncMock, return_value=[]
        ),
        patch(
            "scout.main.cg_fetch_top_movers", new_callable=AsyncMock, return_value=[]
        ),
        patch("scout.main.cg_fetch_trending", new_callable=AsyncMock, return_value=[]),
        patch(
            "scout.main.enrich_holders",
            new_callable=AsyncMock,
            side_effect=lambda t, s, st: t,
        ),
        patch("scout.main.aggregate", return_value=[token]),
        patch("scout.main.score", return_value=(75, ["vol_liq_ratio"])),
        patch(
            "scout.main.evaluate",
            new_callable=AsyncMock,
            return_value=(True, 78.0, token),
        ),
        patch("scout.main.is_safe", new_callable=AsyncMock, return_value=False),
        patch("scout.main.send_alert", new_callable=AsyncMock) as mock_alert,
    ):

        stats = await run_cycle(mock_settings, mock_db, mock_session, dry_run=False)

    mock_alert.assert_not_called()
