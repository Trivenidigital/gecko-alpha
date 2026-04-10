"""Tests for conviction chain Pydantic models."""
from datetime import datetime, timezone

from scout.chains.models import (
    ActiveChain,
    ChainEvent,
    ChainMatch,
    ChainPattern,
    ChainStep,
)


def test_chain_event_required_fields():
    ev = ChainEvent(
        token_id="0xabc",
        pipeline="memecoin",
        event_type="candidate_scored",
        event_data={"quant_score": 72, "signal_count": 3},
        source_module="scorer",
        created_at=datetime.now(timezone.utc),
    )
    assert ev.id is None
    assert ev.pipeline == "memecoin"
    assert ev.event_data["signal_count"] == 3


def test_chain_step_optional_condition():
    s = ChainStep(
        step_number=1,
        event_type="category_heating",
        max_hours_after_anchor=0.0,
    )
    assert s.condition is None
    assert s.max_hours_after_previous is None


def test_chain_pattern_with_steps():
    pat = ChainPattern(
        name="test_pattern",
        description="A test pattern",
        steps=[
            ChainStep(
                step_number=1,
                event_type="category_heating",
                max_hours_after_anchor=0.0,
            ),
            ChainStep(
                step_number=2,
                event_type="laggard_picked",
                max_hours_after_anchor=6.0,
            ),
        ],
        min_steps_to_trigger=2,
        conviction_boost=25,
        alert_priority="high",
    )
    assert pat.is_active is True
    assert pat.total_triggers == 0
    assert pat.historical_hit_rate is None


def test_active_chain_tracking():
    now = datetime.now(timezone.utc)
    ac = ActiveChain(
        token_id="0xabc",
        pipeline="memecoin",
        pattern_id=1,
        pattern_name="full_conviction",
        steps_matched=[1, 2],
        step_events={1: 10, 2: 15},
        anchor_time=now,
        last_step_time=now,
        created_at=now,
    )
    assert ac.is_complete is False
    assert ac.step_events[1] == 10


def test_chain_match_outcome_nullable():
    now = datetime.now(timezone.utc)
    cm = ChainMatch(
        token_id="0xabc",
        pipeline="memecoin",
        pattern_id=1,
        pattern_name="full_conviction",
        steps_matched=3,
        total_steps=4,
        anchor_time=now,
        completed_at=now,
        chain_duration_hours=4.5,
        conviction_boost=25,
    )
    assert cm.outcome_class is None
    assert cm.evaluated_at is None
