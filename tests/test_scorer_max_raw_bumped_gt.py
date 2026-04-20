"""Pin SCORER_MAX_RAW after BL-054 recalibration (+10 for perp_anomaly)."""

from scout import scorer


def test_scorer_max_raw_bumped_for_gt_trending():
    assert scorer.SCORER_MAX_RAW == 208
