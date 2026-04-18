"""Tests for scout.social.lunarcrush.alerter -- Telegram format."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.social.lunarcrush.alerter import format_social_alert
from scout.social.models import ResearchAlert, SpikeKind


def _alert(**overrides) -> ResearchAlert:
    defaults = dict(
        coin_id="astro",
        symbol="AST",
        name="Asteroid",
        spike_kinds=[SpikeKind.SOCIAL_VOLUME_24H],
        social_spike_ratio=4.2,
        galaxy_score=72.0,
        galaxy_jump=14.0,
        social_volume_24h=50_000.0,
        social_volume_baseline=12_000.0,
        interactions_24h=31_000.0,
        interactions_ratio=5.0,
        sentiment=0.82,
        social_dominance=0.04,
        price_change_1h=42.1,
        price_change_24h=114_775.8,
        market_cap=24_300_000.0,
        current_price=0.00006,
        detected_at=datetime(2026, 4, 18, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return ResearchAlert(**defaults)


def test_message_header_and_url_present():
    msg = format_social_alert([_alert()])
    assert "*Social Velocity*" in msg
    assert "lunarcrush.com/coins/astro" in msg


def test_multi_kind_lists_all_three():
    alert = _alert(
        spike_kinds=[
            SpikeKind.SOCIAL_VOLUME_24H,
            SpikeKind.GALAXY_JUMP,
            SpikeKind.INTERACTIONS_ACCEL,
        ]
    )
    msg = format_social_alert([alert])
    assert "social_volume_24h" in msg
    assert "galaxy_jump" in msg
    assert "interactions_accel" in msg


def test_markdown_escape_of_underscore_symbol():
    """A symbol like AS_ROID does not break Markdown parse mode."""
    alert = _alert(symbol="AS_ROID", name="Asteroid_Shiba")
    msg = format_social_alert([alert])
    assert r"AS\_ROID" in msg
    assert r"Asteroid\_Shiba" in msg


def test_missing_price_renders_em_dash():
    alert = _alert(current_price=None, price_change_1h=None, price_change_24h=None)
    msg = format_social_alert([alert])
    # Renders an em dash for the missing price line rather than blowing up.
    assert "\u2014" in msg or "—" in msg


def test_truncation_at_4096():
    """A very-long batched message is truncated at Telegram's 4096-char cap."""
    alerts = [_alert(name="X" * 200) for _ in range(50)]
    msg = format_social_alert(alerts)
    assert len(msg) <= 4096


def test_cg_chart_link_omitted_when_slug_missing():
    """Numeric LunarCrush coin_ids must NOT build a coingecko.com URL that 404s."""
    alert = _alert(coin_id="12345", cg_slug=None)
    msg = format_social_alert([alert])
    # LunarCrush link still renders using the LC-native coin_id.
    assert "lunarcrush.com/coins/12345" in msg
    # No fake CoinGecko chart link is emitted.
    assert "coingecko.com/en/coins/12345" not in msg
    assert "[chart]" not in msg


def test_cg_chart_link_uses_slug_when_present():
    """When cg_slug is set, the chart link uses the CG slug, not the coin_id."""
    alert = _alert(coin_id="12345", cg_slug="asteroid-shiba")
    msg = format_social_alert([alert])
    assert "coingecko.com/en/coins/asteroid-shiba" in msg
    assert "coingecko.com/en/coins/12345" not in msg
