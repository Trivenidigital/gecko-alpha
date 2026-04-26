"""Deterministic table-driven tests for the moonshot/baseline trail formula.

Extracted from the evaluator hot path to `compute_trail_threshold(peak, drawdown)`
so its invariants can be exercised independently of any DB or evaluator state.

These cover the property-style invariants the test-coverage reviewer flagged
without pulling in a hypothesis dependency.
"""

from __future__ import annotations

import pytest

from scout.trading.evaluator import compute_trail_threshold


@pytest.mark.parametrize(
    "peak,drawdown,expected",
    [
        (100.0, 30.0, 70.0),
        (100.0, 12.0, 88.0),
        (1.5, 30.0, pytest.approx(1.05, rel=1e-9)),
        (0.001, 50.0, pytest.approx(0.0005, rel=1e-9)),
        (1e6, 1.0, pytest.approx(990_000.0, rel=1e-9)),
    ],
)
def test_trail_threshold_known_values(peak, drawdown, expected):
    assert compute_trail_threshold(peak, drawdown) == expected


@pytest.mark.parametrize(
    "peak,drawdown",
    [
        (1.0, 1.0),
        (1.0, 50.0),
        (1.0, 99.99),
        (1e-6, 30.0),
        (1e9, 30.0),
    ],
)
def test_trail_threshold_below_peak_for_valid_drawdown(peak, drawdown):
    """For 0 < drawdown < 100, the trail is strictly below peak and > 0."""
    threshold = compute_trail_threshold(peak, drawdown)
    assert 0 < threshold < peak


def test_trail_threshold_widening_drawdown_lowers_threshold():
    """Monotonic invariant: a wider drawdown produces a lower trail threshold."""
    peak = 1.50
    thresholds = [compute_trail_threshold(peak, dd) for dd in (5, 12, 20, 30, 50)]
    # Strictly decreasing
    for a, b in zip(thresholds, thresholds[1:]):
        assert b < a
