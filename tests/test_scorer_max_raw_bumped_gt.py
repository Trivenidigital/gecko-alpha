"""Pin SCORER_MAX_RAW after dead social denominator removal."""

from scout import scorer


def test_scorer_max_raw_bumped_for_gt_trending():
    assert scorer.SCORER_MAX_RAW == 193
