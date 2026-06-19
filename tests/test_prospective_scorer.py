"""Pure prospective conviction scorer (Task 3): age-based sustained vs fresh."""

from scout.conviction.cross_surface import SURFACE_LEAD_COLUMNS
from scout.conviction.prospective_scorer import score_prospective


def test_four_sustained_is_high(settings_factory):
    s = settings_factory()
    r = score_prospective(
        {"chains": 2000, "spikes": 1500, "momentum": 3000, "velocity": 1440}, s
    )
    assert r.early_count == 4
    assert r.tier == "high"
    assert set(r.contributing) == {"chains", "spikes", "momentum", "velocity"}
    assert r.fresh_count == 0


def test_threshold_inclusive(settings_factory):
    s = settings_factory()  # CONVICTION_EARLY_LEAD_MINUTES = 1440
    r = score_prospective({"chains": 1440}, s)
    assert r.early_count == 1  # exactly at threshold counts


def test_fresh_not_counted_in_tier(settings_factory):
    s = settings_factory()
    r = score_prospective(
        {"chains": 2000, "spikes": 2000, "momentum": 100, "velocity": 50}, s
    )
    assert r.early_count == 2  # only the two >= 1440
    assert r.fresh_count == 2  # the two < 1440 (emerging)
    assert r.tier == "watch"


def test_none_and_negative_ignored(settings_factory):
    s = settings_factory()
    r = score_prospective({"chains": None, "spikes": -5, "momentum": 2000}, s)
    assert r.early_count == 1
    assert r.fresh_count == 0


def test_zero_is_low(settings_factory):
    s = settings_factory()
    r = score_prospective({}, s)
    assert r.early_count == 0 and r.fresh_count == 0 and r.tier == "low"


def test_contributing_in_surface_order(settings_factory):
    s = settings_factory()
    ages = {name: 2000 for name in SURFACE_LEAD_COLUMNS}
    r = score_prospective(ages, s)
    assert list(r.contributing) == list(SURFACE_LEAD_COLUMNS.keys())
    assert r.early_count == 8 and r.tier == "high"
