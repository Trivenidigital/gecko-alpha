"""Test gt_trending scoring signal (BL-052)."""

import pytest
import structlog
from structlog.testing import capture_logs

from scout.config import Settings
from scout.models import CandidateToken
from scout.scorer import score


@pytest.fixture
def settings():
    return Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )


def _tok(**overrides):
    defaults = dict(
        contract_address="0xabc",
        chain="base",
        token_name="Test",
        ticker="TST",
        market_cap_usd=50_000.0,
        liquidity_usd=20_000.0,
        volume_24h_usd=10_000.0,
        holder_count=50,
        holder_growth_1h=0,
    )
    defaults.update(overrides)
    return CandidateToken(**defaults)


def test_gt_trending_fires_at_rank_1(settings):
    token = _tok(gt_trending_rank=1)
    _, signals = score(token, settings)
    assert "gt_trending" in signals


def test_gt_trending_does_not_fire_at_rank_11_default_top_n_10(settings):
    token = _tok(gt_trending_rank=11)
    _, signals = score(token, settings)
    assert "gt_trending" not in signals


def test_gt_trending_skipped_when_rank_none(settings):
    token = _tok(gt_trending_rank=None)
    _, signals = score(token, settings)
    assert "gt_trending" not in signals


def test_gt_trending_boundary_top_n_3(settings):
    strict = settings.model_copy(update={"GT_TRENDING_TOP_N": 3})
    assert "gt_trending" in score(_tok(gt_trending_rank=3), strict)[1]
    assert "gt_trending" not in score(_tok(gt_trending_rank=4), strict)[1]


def test_gt_trending_fires_logs_event(settings):
    token = _tok(gt_trending_rank=2, ticker="ROCKET", contract_address="0xdead")
    with capture_logs() as logs:
        score(token, settings)
    events = [e for e in logs if e.get("event") == "gt_trending_signal_fired"]
    assert len(events) == 1
    e = events[0]
    assert e["token"] == "ROCKET"
    assert e["contract_address"] == "0xdead"
    assert e["chain"] == "base"
    assert e["gt_trending_rank"] == 2


def test_gt_trending_silent_below_threshold_no_log(settings):
    token = _tok(gt_trending_rank=99)
    with capture_logs() as logs:
        score(token, settings)
    events = [e for e in logs if e.get("event") == "gt_trending_signal_fired"]
    assert events == []
