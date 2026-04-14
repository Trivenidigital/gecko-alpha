"""Tests for trending tracker Pydantic models."""

from datetime import datetime, timezone

from scout.trending.models import TrendingComparison, TrendingSnapshot, TrendingStats


def test_trending_snapshot_defaults():
    now = datetime.now(timezone.utc)
    snap = TrendingSnapshot(
        coin_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        snapshot_at=now,
    )
    assert snap.coin_id == "bitcoin"
    assert snap.market_cap_rank is None
    assert snap.trending_score is None
    assert snap.snapshot_at == now


def test_trending_snapshot_full():
    now = datetime.now(timezone.utc)
    snap = TrendingSnapshot(
        coin_id="bless-network",
        symbol="BLESS",
        name="Bless Network",
        market_cap_rank=42,
        trending_score=3.0,
        snapshot_at=now,
    )
    assert snap.market_cap_rank == 42
    assert snap.trending_score == 3.0


def test_trending_comparison_defaults():
    now = datetime.now(timezone.utc)
    comp = TrendingComparison(
        coin_id="test",
        symbol="TST",
        name="Test",
        appeared_on_trending_at=now,
    )
    assert comp.is_gap is True
    assert comp.detected_by_narrative is False
    assert comp.detected_by_pipeline is False
    assert comp.detected_by_chains is False
    assert comp.narrative_lead_minutes is None


def test_trending_comparison_caught():
    now = datetime.now(timezone.utc)
    comp = TrendingComparison(
        coin_id="test",
        symbol="TST",
        name="Test",
        appeared_on_trending_at=now,
        detected_by_narrative=True,
        narrative_lead_minutes=45.5,
        is_gap=False,
    )
    assert comp.is_gap is False
    assert comp.narrative_lead_minutes == 45.5


def test_trending_stats_defaults():
    stats = TrendingStats()
    assert stats.total_tracked == 0
    assert stats.hit_rate_pct == 0.0
    assert stats.avg_lead_minutes is None


def test_trending_stats_populated():
    stats = TrendingStats(
        total_tracked=20,
        caught_before_trending=8,
        missed=12,
        hit_rate_pct=40.0,
        avg_lead_minutes=120.5,
        best_lead_minutes=30.0,
        by_narrative=3,
        by_pipeline=5,
        by_chains=2,
    )
    assert stats.hit_rate_pct == 40.0
    assert stats.best_lead_minutes == 30.0
