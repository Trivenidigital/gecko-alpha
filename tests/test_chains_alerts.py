"""Tests for chain alert formatting."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from scout.chains.alerts import format_chain_alert, send_chain_alert
from scout.chains.models import ActiveChain, ChainPattern, ChainStep


def _make_pattern() -> ChainPattern:
    return ChainPattern(
        id=1,
        name="full_conviction",
        description="test",
        steps=[
            ChainStep(
                step_number=1, event_type="category_heating", max_hours_after_anchor=0.0
            ),
            ChainStep(
                step_number=2, event_type="laggard_picked", max_hours_after_anchor=6.0
            ),
            ChainStep(
                step_number=3, event_type="counter_scored", max_hours_after_anchor=8.0
            ),
            ChainStep(
                step_number=4,
                event_type="candidate_scored",
                max_hours_after_anchor=12.0,
            ),
        ],
        min_steps_to_trigger=3,
        conviction_boost=25,
        alert_priority="high",
        historical_hit_rate=0.42,
        total_triggers=17,
        total_hits=7,
    )


def test_format_chain_alert_contains_required_fields():
    now = datetime.now(timezone.utc)
    chain = ActiveChain(
        token_id="0xabc",
        pipeline="memecoin",
        pattern_id=1,
        pattern_name="full_conviction",
        steps_matched=[1, 2, 3],
        step_events={1: 10, 2: 11, 3: 12},
        anchor_time=now,
        last_step_time=now + timedelta(hours=3),
        is_complete=True,
        completed_at=now + timedelta(hours=3),
        created_at=now,
    )
    msg = format_chain_alert(chain, _make_pattern())
    assert "CONVICTION CHAIN COMPLETE" in msg
    assert "full_conviction" in msg
    assert "3/4" in msg
    assert "+25" in msg
    assert "42" in msg


@pytest.mark.asyncio
async def test_send_chain_alert_uses_plain_text_parse_mode(monkeypatch):
    now = datetime.now(timezone.utc)
    chain = ActiveChain(
        token_id="0xabc",
        pipeline="memecoin",
        pattern_id=1,
        pattern_name="full_conviction",
        steps_matched=[1, 2, 3],
        step_events={1: 10, 2: 11, 3: 12},
        anchor_time=now,
        last_step_time=now + timedelta(hours=3),
        is_complete=True,
        completed_at=now + timedelta(hours=3),
        created_at=now,
    )
    captured = {}

    async def _send(text, session, settings, **kwargs):
        captured["text"] = text
        captured["kwargs"] = kwargs

    monkeypatch.setattr("scout.alerter.send_telegram_message", _send)

    await send_chain_alert(
        db=SimpleNamespace(),
        chain=chain,
        pattern=_make_pattern(),
        settings=SimpleNamespace(),
    )

    assert "full_conviction" in captured["text"]
    assert captured["kwargs"]["parse_mode"] is None
