"""Tests for narrative rotation agent Pydantic models."""

from datetime import datetime, timezone

import pytest

from scout.narrative.models import (
    CategoryAcceleration,
    CategorySnapshot,
    LaggardToken,
    LearnLog,
    NarrativePrediction,
    NarrativeSignal,
    StrategyValue,
)

NOW = datetime.now(tz=timezone.utc)


class TestCategorySnapshot:
    def test_required_fields(self) -> None:
        snap = CategorySnapshot(
            category_id="defi",
            name="DeFi",
            market_cap=1_000_000.0,
            market_cap_change_24h=5.2,
            volume_24h=500_000.0,
            snapshot_at=NOW,
        )
        assert snap.category_id == "defi"
        assert snap.coin_count is None
        assert snap.market_regime is None

    def test_optional_fields(self) -> None:
        snap = CategorySnapshot(
            category_id="gaming",
            name="Gaming",
            market_cap=2_000_000.0,
            market_cap_change_24h=-1.3,
            volume_24h=800_000.0,
            coin_count=42,
            market_regime="bull",
            snapshot_at=NOW,
        )
        assert snap.coin_count == 42
        assert snap.market_regime == "bull"


class TestCategoryAcceleration:
    def test_is_heating(self) -> None:
        accel = CategoryAcceleration(
            category_id="ai",
            name="AI",
            current_velocity=10.0,
            previous_velocity=5.0,
            acceleration=5.0,
            volume_24h=1_000_000.0,
            volume_growth_pct=80.0,
            is_heating=True,
        )
        assert accel.is_heating is True
        assert accel.coin_count_change is None

    def test_not_heating(self) -> None:
        accel = CategoryAcceleration(
            category_id="meme",
            name="Meme",
            current_velocity=2.0,
            previous_velocity=8.0,
            acceleration=-6.0,
            volume_24h=300_000.0,
            volume_growth_pct=-20.0,
            coin_count_change=-3,
            is_heating=False,
        )
        assert accel.is_heating is False
        assert accel.coin_count_change == -3


class TestNarrativePrediction:
    def test_defaults(self) -> None:
        pred = NarrativePrediction(
            category_id="defi",
            category_name="DeFi",
            coin_id="token-abc",
            symbol="ABC",
            name="AbcToken",
            market_cap_at_prediction=100_000.0,
            price_at_prediction=0.05,
            narrative_fit_score=75,
            staying_power="medium",
            confidence="high",
            reasoning="Strong momentum in DeFi category.",
            market_regime="bull",
            trigger_count=2,
            strategy_snapshot={"min_accel": 3.0},
            predicted_at=NOW,
        )
        assert pred.is_control is False
        assert pred.is_holdout is False
        assert pred.outcome_class is None
        assert pred.outcome_6h_price is None
        assert pred.evaluated_at is None
        assert pred.id is None

    def test_control_pick(self) -> None:
        pred = NarrativePrediction(
            category_id="gaming",
            category_name="Gaming",
            coin_id="token-xyz",
            symbol="XYZ",
            name="XyzToken",
            market_cap_at_prediction=50_000.0,
            price_at_prediction=0.01,
            narrative_fit_score=60,
            staying_power="low",
            confidence="medium",
            reasoning="Control pick for baseline.",
            market_regime="sideways",
            trigger_count=1,
            is_control=True,
            strategy_snapshot={"min_accel": 3.0},
            predicted_at=NOW,
        )
        assert pred.is_control is True

    def test_with_outcomes(self) -> None:
        pred = NarrativePrediction(
            category_id="defi",
            category_name="DeFi",
            coin_id="token-abc",
            symbol="ABC",
            name="AbcToken",
            market_cap_at_prediction=100_000.0,
            price_at_prediction=0.05,
            narrative_fit_score=75,
            staying_power="medium",
            confidence="high",
            reasoning="Test",
            market_regime="bull",
            trigger_count=2,
            strategy_snapshot={},
            predicted_at=NOW,
            outcome_6h_price=0.06,
            outcome_6h_change_pct=20.0,
            outcome_6h_class="hit",
            outcome_class="hit",
            evaluated_at=NOW,
        )
        assert pred.outcome_6h_class == "hit"
        assert pred.outcome_class == "hit"


class TestStrategyValue:
    def test_creation(self) -> None:
        sv = StrategyValue(
            key="MIN_ACCELERATION",
            value="3.0",
            updated_at=NOW,
            updated_by="learn_cycle",
            reason="Initial default",
        )
        assert sv.locked is False
        assert sv.min_bound is None
        assert sv.max_bound is None

    def test_locked_with_bounds(self) -> None:
        sv = StrategyValue(
            key="MIN_VOLUME_GROWTH",
            value="50.0",
            updated_at=NOW,
            updated_by="human",
            reason="Manually locked",
            locked=True,
            min_bound=10.0,
            max_bound=200.0,
        )
        assert sv.locked is True
        assert sv.min_bound == 10.0


class TestNarrativeSignal:
    def test_default_trigger_count(self) -> None:
        sig = NarrativeSignal(
            category_id="defi",
            category_name="DeFi",
            acceleration=5.0,
            volume_growth_pct=80.0,
            detected_at=NOW,
            cooling_down_until=NOW,
        )
        assert sig.trigger_count == 1
        assert sig.coin_count_change is None

    def test_custom_trigger_count(self) -> None:
        sig = NarrativeSignal(
            category_id="ai",
            category_name="AI",
            acceleration=8.0,
            volume_growth_pct=120.0,
            coin_count_change=5,
            trigger_count=3,
            detected_at=NOW,
            cooling_down_until=NOW,
        )
        assert sig.trigger_count == 3
        assert sig.coin_count_change == 5


class TestLearnLog:
    def test_creation(self) -> None:
        log = LearnLog(
            cycle_number=1,
            cycle_type="daily",
            reflection_text="Adjusted acceleration threshold.",
            changes_made={"MIN_ACCELERATION": {"old": 3.0, "new": 2.5}},
            hit_rate_before=0.45,
            created_at=NOW,
        )
        assert log.id is None
        assert log.hit_rate_after is None
        assert log.cycle_type == "daily"

    def test_with_after_rate(self) -> None:
        log = LearnLog(
            cycle_number=2,
            cycle_type="weekly",
            reflection_text="Weekly review.",
            changes_made={},
            hit_rate_before=0.50,
            hit_rate_after=0.55,
            created_at=NOW,
        )
        assert log.hit_rate_after == 0.55
