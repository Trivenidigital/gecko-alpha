"""Pin the GT_TRENDING_TOP_N default (BL-052)."""

import pytest

from scout.config import Settings


@pytest.fixture
def base_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")


def test_gt_trending_top_n_default(base_env):
    s = Settings()
    assert s.GT_TRENDING_TOP_N == 10


def test_gt_trending_top_n_override(base_env, monkeypatch):
    monkeypatch.setenv("GT_TRENDING_TOP_N", "3")
    s = Settings()
    assert s.GT_TRENDING_TOP_N == 3
