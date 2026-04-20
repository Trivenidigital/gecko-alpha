"""Pin SCORER_MAX_RAW after BL-052 gt_trending signal (+15)."""

from scout import scorer


def test_scorer_max_raw_bumped_for_gt_trending():
    assert scorer.SCORER_MAX_RAW == 198
