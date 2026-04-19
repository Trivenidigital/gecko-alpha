"""Tests for conviction gate."""

from unittest.mock import AsyncMock, patch

import pytest

from scout.gate import evaluate
from scout.models import MiroFishResult


@pytest.fixture
def mock_db():
    db = AsyncMock()
    db.get_daily_mirofish_count.return_value = 0
    db.log_mirofish_job = AsyncMock()
    return db


@pytest.fixture
def mock_session():
    return AsyncMock()


async def test_gate_fires_above_threshold(
    mock_db, mock_session, settings_factory, token_factory
):
    """conviction = 75*0.6 + 80*0.4 = 45+32 = 77 >= 70 -> fire."""
    token = token_factory(quant_score=75)
    settings = settings_factory(
        CONVICTION_THRESHOLD=70,
        QUANT_WEIGHT=0.6,
        NARRATIVE_WEIGHT=0.4,
        MIN_SCORE=60,
        MAX_MIROFISH_JOBS_PER_DAY=50,
    )

    with (
        patch("scout.gate.simulate", new_callable=AsyncMock) as mock_sim,
        patch("scout.gate.build_seed") as mock_seed,
    ):
        mock_seed.return_value = {"prompt": "test"}
        mock_sim.return_value = MiroFishResult(
            narrative_score=80, virality_class="High", summary="Viral"
        )
        should_alert, conviction, token_out = await evaluate(
            token, mock_db, mock_session, settings
        )

    assert should_alert is True
    assert conviction == pytest.approx(77.0)


async def test_gate_rejects_below_threshold(
    mock_db, mock_session, settings_factory, token_factory
):
    """conviction = 60*0.6 + 20*0.4 = 36+8 = 44 < 70 -> no fire."""
    token = token_factory(quant_score=60)
    settings = settings_factory(
        CONVICTION_THRESHOLD=70,
        QUANT_WEIGHT=0.6,
        NARRATIVE_WEIGHT=0.4,
        MIN_SCORE=60,
        MAX_MIROFISH_JOBS_PER_DAY=50,
    )

    with (
        patch("scout.gate.simulate", new_callable=AsyncMock) as mock_sim,
        patch("scout.gate.build_seed") as mock_seed,
    ):
        mock_seed.return_value = {"prompt": "test"}
        mock_sim.return_value = MiroFishResult(
            narrative_score=20, virality_class="Low", summary="Weak"
        )
        should_alert, conviction, token_out = await evaluate(
            token, mock_db, mock_session, settings
        )

    assert should_alert is False
    assert conviction == pytest.approx(44.0)


async def test_gate_boundary_exactly_70(
    mock_db, mock_session, settings_factory, token_factory
):
    """Exactly at threshold -> fire."""
    # Need: quant*0.6 + narrative*0.4 = 70
    # quant=100, narrative=25: 60+10=70
    token = token_factory(quant_score=100)
    settings = settings_factory(
        CONVICTION_THRESHOLD=70,
        QUANT_WEIGHT=0.6,
        NARRATIVE_WEIGHT=0.4,
        MIN_SCORE=60,
        MAX_MIROFISH_JOBS_PER_DAY=50,
    )

    with (
        patch("scout.gate.simulate", new_callable=AsyncMock) as mock_sim,
        patch("scout.gate.build_seed") as mock_seed,
    ):
        mock_seed.return_value = {"prompt": "test"}
        mock_sim.return_value = MiroFishResult(
            narrative_score=25, virality_class="Low", summary="Weak"
        )
        should_alert, conviction, token_out = await evaluate(
            token, mock_db, mock_session, settings
        )

    assert should_alert is True
    assert conviction == pytest.approx(70.0)


async def test_gate_daily_cap_skips_mirofish(
    mock_db, mock_session, settings_factory, token_factory
):
    """At daily cap -> skip MiroFish, use quant-only score."""
    mock_db.get_daily_mirofish_count.return_value = 50  # at cap
    token = token_factory(quant_score=75)
    settings = settings_factory(
        CONVICTION_THRESHOLD=70,
        QUANT_WEIGHT=0.6,
        NARRATIVE_WEIGHT=0.4,
        MIN_SCORE=60,
        MAX_MIROFISH_JOBS_PER_DAY=50,
    )

    should_alert, conviction, token_out = await evaluate(
        token, mock_db, mock_session, settings
    )

    assert conviction == 75.0  # quant-only
    assert should_alert is True  # 75 >= 70


async def test_gate_below_min_score_skips_mirofish(
    mock_db, mock_session, settings_factory, token_factory
):
    """quant_score < MIN_SCORE -> skip MiroFish."""
    token = token_factory(quant_score=40)
    settings = settings_factory(
        CONVICTION_THRESHOLD=70,
        QUANT_WEIGHT=0.6,
        NARRATIVE_WEIGHT=0.4,
        MIN_SCORE=60,
        MAX_MIROFISH_JOBS_PER_DAY=50,
    )

    should_alert, conviction, token_out = await evaluate(
        token, mock_db, mock_session, settings
    )

    assert conviction == 40.0  # quant-only, no MiroFish
    assert should_alert is False  # 40 < 70


async def test_gate_mirofish_fallback_on_timeout(
    mock_db, mock_session, settings_factory, token_factory
):
    """MiroFish timeout -> fallback to Anthropic."""
    from scout.exceptions import MiroFishTimeoutError

    token = token_factory(quant_score=80)
    settings = settings_factory(
        CONVICTION_THRESHOLD=70,
        QUANT_WEIGHT=0.6,
        NARRATIVE_WEIGHT=0.4,
        MIN_SCORE=60,
        MAX_MIROFISH_JOBS_PER_DAY=50,
    )

    with (
        patch("scout.gate.simulate", new_callable=AsyncMock) as mock_sim,
        patch("scout.gate.build_seed") as mock_seed,
        patch(
            "scout.gate.score_narrative_fallback", new_callable=AsyncMock
        ) as mock_fallback,
    ):
        mock_seed.return_value = {"prompt": "test"}
        mock_sim.side_effect = MiroFishTimeoutError("timeout")
        mock_fallback.return_value = MiroFishResult(
            narrative_score=70, virality_class="High", summary="Fallback"
        )
        should_alert, conviction, token_out = await evaluate(
            token, mock_db, mock_session, settings
        )

    # conviction = 80*0.6 + 70*0.4 = 48+28 = 76
    assert conviction == pytest.approx(76.0)
    assert should_alert is True
    mock_fallback.assert_called_once()
    # Job logged AFTER successful fallback, not before simulation
    mock_db.log_mirofish_job.assert_called_once_with("0xtest")
