"""Tests that pin SCORER_MAX_RAW == 203 after BL-051 bump."""

from scout.scorer import SCORER_MAX_RAW


def test_scorer_max_raw_is_203():
    # 30+8+25+15+15+15+20+25+15+20+5+10 = 203
    assert SCORER_MAX_RAW == 203
