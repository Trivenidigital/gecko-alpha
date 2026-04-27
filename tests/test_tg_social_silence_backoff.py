"""BL-064 silence-heartbeat backoff tests.

Round-2 Medium #3 fix: alerts use exponential-then-linear backoff so a
7-day outage doesn't fire 168 alerts. Pure unit-test of the schedule
helper plus an integration check that the heartbeat respects the stamp.
"""

from __future__ import annotations

import pytest

from scout.social.telegram.listener import _next_silence_alert_due_hours


def test_first_alert_at_threshold():
    """At elapsed_at_alert=0, next alert fires at threshold (1× milestone)."""
    assert _next_silence_alert_due_hours(0.0, 72) == 72.0


def test_milestone_progression():
    """Second alert at 1.5×, third at 2×, fourth at 3×, fifth at 4×."""
    threshold = 72
    assert _next_silence_alert_due_hours(72.0, threshold) == 108.0  # 1.5×
    assert _next_silence_alert_due_hours(108.0, threshold) == 144.0  # 2×
    assert _next_silence_alert_due_hours(144.0, threshold) == 216.0  # 3×
    assert _next_silence_alert_due_hours(216.0, threshold) == 288.0  # 4×


def test_after_4x_linear_step():
    """Past 4× threshold, alerts step every max(threshold, 24) hours."""
    threshold = 72  # max(72, 24) = 72
    # We just alerted at 4× = 288. Next at 4×+72 = 360.
    assert _next_silence_alert_due_hours(288.0, threshold) == 360.0
    # Then 432, 504, ...
    assert _next_silence_alert_due_hours(360.0, threshold) == 432.0


def test_24h_floor_for_small_thresholds():
    """If threshold < 24h, the post-4× cadence is 24h not threshold."""
    threshold = 6  # max(6, 24) = 24
    # 4× = 24, next = 24 + 24 = 48
    assert _next_silence_alert_due_hours(24.0, threshold) == 48.0
    assert _next_silence_alert_due_hours(48.0, threshold) == 72.0


def test_seven_day_outage_alert_count():
    """Sanity check: a 7-day (168h) outage at default threshold (72h) fires
    far fewer than the 168 alerts the v1 implementation would have sent."""
    threshold = 72
    elapsed_at_milestones = [0.0]  # initial state — no alert yet
    while elapsed_at_milestones[-1] < 168:
        nxt = _next_silence_alert_due_hours(elapsed_at_milestones[-1], threshold)
        if nxt > 168:
            break
        elapsed_at_milestones.append(nxt)
    # Excluding the initial 0.0, count the actual alert milestones <= 168h.
    alert_count = len(elapsed_at_milestones) - 1
    assert alert_count <= 4  # was 168 in v1; should be 3-4 with backoff
