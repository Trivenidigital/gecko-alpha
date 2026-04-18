"""Tests for scout.social.models -- ResearchAlert + SpikeKind + BaselineState."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scout.social.models import BaselineState, ResearchAlert, SpikeKind


def test_spike_kind_enum_values():
    """Three kinds exactly; string values match DB column suffixes."""
    assert SpikeKind.SOCIAL_VOLUME_24H.value == "social_volume_24h"
    assert SpikeKind.GALAXY_JUMP.value == "galaxy_jump"
    assert SpikeKind.INTERACTIONS_ACCEL.value == "interactions_accel"
    assert len(list(SpikeKind)) == 3


def test_research_alert_required_fields():
    """ResearchAlert carries coin identity + triggered kinds."""
    alert = ResearchAlert(
        coin_id="astro-shiba",
        symbol="AST",
        name="Asteroid Shiba",
        spike_kinds=[SpikeKind.SOCIAL_VOLUME_24H],
        galaxy_score=72.0,
        social_volume_24h=50_000.0,
        social_volume_baseline=12_000.0,
        social_spike_ratio=4.2,
        interactions_24h=31_000.0,
        sentiment=0.82,
        social_dominance=0.04,
        price_change_1h=42.1,
        price_change_24h=114_775.8,
        market_cap=24_300_000.0,
        current_price=0.00006,
        detected_at=datetime(2026, 4, 18, tzinfo=timezone.utc),
    )
    assert alert.coin_id == "astro-shiba"
    assert SpikeKind.SOCIAL_VOLUME_24H in alert.spike_kinds
    assert alert.social_spike_ratio == 4.2


def test_research_alert_supports_multi_kind():
    """A single alert can carry multiple spike kinds."""
    alert = ResearchAlert(
        coin_id="foo",
        symbol="FOO",
        name="Foo",
        spike_kinds=[SpikeKind.SOCIAL_VOLUME_24H, SpikeKind.GALAXY_JUMP],
        detected_at=datetime.now(timezone.utc),
    )
    assert len(alert.spike_kinds) == 2


def test_baseline_state_defaults():
    """BaselineState starts zeroed with an empty interactions ring."""
    state = BaselineState(
        coin_id="foo",
        symbol="FOO",
        avg_social_volume_24h=0.0,
        avg_galaxy_score=0.0,
        last_galaxy_score=None,
        interactions_ring=[],
        sample_count=0,
        last_poll_at=None,
        last_updated=datetime(2026, 4, 18, tzinfo=timezone.utc),
    )
    assert state.sample_count == 0
    assert state.interactions_ring == []


def test_baseline_state_is_immutable_via_replace():
    """BaselineState is replaceable (NamedTuple / dataclass -> _replace-style)."""
    now = datetime.now(timezone.utc)
    state = BaselineState(
        coin_id="foo",
        symbol="FOO",
        avg_social_volume_24h=1.0,
        avg_galaxy_score=50.0,
        last_galaxy_score=50.0,
        interactions_ring=[1.0, 2.0],
        sample_count=1,
        last_poll_at=now,
        last_updated=now,
    )
    updated = state._replace(sample_count=2)
    assert updated.sample_count == 2
    assert state.sample_count == 1  # original unchanged
