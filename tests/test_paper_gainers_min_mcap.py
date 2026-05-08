"""Tests for PAPER_GAINERS_MIN_MCAP override of global PAPER_MIN_MCAP.

See tasks/plan_paper_gainers_min_mcap_3m.md.
"""

import pytest

from scout.config import Settings


def _resolve_gainers_min_mcap(settings: Settings) -> float:
    """Mirror of the resolver in scout/main.py for trade_gainers dispatch."""
    return (
        settings.PAPER_GAINERS_MIN_MCAP
        if settings.PAPER_GAINERS_MIN_MCAP is not None
        else settings.PAPER_MIN_MCAP
    )


def test_gainers_min_mcap_falls_back_to_global_when_unset(settings_factory):
    s = settings_factory(PAPER_MIN_MCAP=5_000_000, PAPER_GAINERS_MIN_MCAP=None)
    assert _resolve_gainers_min_mcap(s) == 5_000_000


def test_gainers_min_mcap_uses_override_when_set(settings_factory):
    s = settings_factory(PAPER_MIN_MCAP=5_000_000, PAPER_GAINERS_MIN_MCAP=3_000_000)
    assert _resolve_gainers_min_mcap(s) == 3_000_000


def test_gainers_min_mcap_default_is_none(settings_factory):
    s = settings_factory()
    assert s.PAPER_GAINERS_MIN_MCAP is None
    assert _resolve_gainers_min_mcap(s) == s.PAPER_MIN_MCAP


def test_gainers_min_mcap_rejects_negative(settings_factory):
    with pytest.raises(ValueError, match="PAPER_GAINERS_MIN_MCAP"):
        settings_factory(PAPER_GAINERS_MIN_MCAP=-1)


def test_gainers_min_mcap_zero_is_allowed(settings_factory):
    """Zero is a sentinel meaning 'no MC floor at all' for gainers — distinct
    from None (= inherit global). Both are valid."""
    s = settings_factory(PAPER_GAINERS_MIN_MCAP=0)
    assert s.PAPER_GAINERS_MIN_MCAP == 0
    assert _resolve_gainers_min_mcap(s) == 0
