"""BL-NEW-CROSS-SURFACE-CONVICTION-SCORE: cross_surface_conviction scorer tests."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from scout.conviction import (
    SURFACE_LEAD_COLUMNS,
    TIER_ORDER,
    cross_surface_conviction,
)


def _settings(**over):
    base = dict(
        CONVICTION_EARLY_LEAD_MINUTES=1440,
        CONVICTION_HIGH_TIER_MIN_SURFACES=4,
        CONVICTION_WATCH_TIER_MIN_SURFACES=2,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _row(early=(), lead=2000.0, **extra):
    """Build a gainers_comparisons-shaped dict. `early` = surfaces detected with
    `lead` minutes; all others are detected=0 / lead=None."""
    r: dict = {}
    for surface, col in SURFACE_LEAD_COLUMNS.items():
        r[f"detected_by_{surface}"] = 1 if surface in early else 0
        r[col] = lead if surface in early else None
    r.update(extra)
    return r


def test_zero_surfaces_is_low_tier():
    res = cross_surface_conviction(_row(), _settings())
    assert res.early_count == 0
    assert res.score == 0.0
    assert res.tier == "low"
    assert res.contributing == ()


def test_one_early_surface_is_low_tier():
    res = cross_surface_conviction(_row(early=("chains",)), _settings())
    assert res.early_count == 1
    assert res.tier == "low"  # watch gate is 2


def test_two_early_surfaces_is_watch_tier():
    res = cross_surface_conviction(_row(early=("chains", "momentum")), _settings())
    assert res.early_count == 2
    assert res.tier == "watch"
    assert set(res.contributing) == {"chains", "momentum"}


def test_four_early_surfaces_is_high_tier():
    res = cross_surface_conviction(
        _row(early=("chains", "momentum", "slow_burn", "velocity")), _settings()
    )
    assert res.early_count == 4
    assert res.score == 4.0
    assert res.tier == "high"


def test_all_eight_surfaces():
    res = cross_surface_conviction(
        _row(early=tuple(SURFACE_LEAD_COLUMNS.keys())), _settings()
    )
    assert res.early_count == 8
    assert res.tier == "high"


def test_lead_exactly_at_threshold_is_inclusive():
    res = cross_surface_conviction(_row(early=("chains",), lead=1440.0), _settings())
    assert res.early_count == 1  # >= threshold counts


def test_lead_below_threshold_excluded():
    res = cross_surface_conviction(_row(early=("chains",), lead=1439.0), _settings())
    assert res.early_count == 0  # fired, but not >=24h early -> not predictive


def test_null_lead_with_detected_does_not_count():
    # detected=1 but lead_minutes is NULL (surface saw it only at/after appearance)
    row = _row()
    row["detected_by_chains"] = 1
    row["chains_lead_minutes"] = None
    res = cross_surface_conviction(row, _settings())
    assert res.early_count == 0


def test_detected_zero_with_lead_present_does_not_count():
    row = _row()
    row["detected_by_chains"] = 0
    row["chains_lead_minutes"] = 9999.0  # stale value, but not detected
    res = cross_surface_conviction(row, _settings())
    assert res.early_count == 0


def test_missing_columns_degrade_not_raise():
    # Partially-populated row (e.g., schema drift) must not raise.
    res = cross_surface_conviction({"detected_by_chains": 1}, _settings())
    assert res.early_count == 0  # chains_lead_minutes missing -> not early
    assert res.tier == "low"


def test_empty_row_degrades():
    res = cross_surface_conviction({}, _settings())
    assert res.early_count == 0


def test_nan_lead_excluded():
    res = cross_surface_conviction(
        _row(early=("chains",), lead=float("nan")), _settings()
    )
    assert res.early_count == 0


def test_works_on_sqlite_row():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE t (detected_by_chains INT, chains_lead_minutes REAL, "
        "detected_by_momentum INT, momentum_lead_minutes REAL)"
    )
    conn.execute(
        "INSERT INTO t VALUES (1, 3000.0, 1, 50.0)"
    )  # chains early, momentum late
    row = conn.execute("SELECT * FROM t").fetchone()
    res = cross_surface_conviction(row, _settings())
    assert res.early_count == 1  # only chains is >=24h early
    assert res.contributing == ("chains",)
    conn.close()


def test_tier_thresholds_configurable():
    s = _settings(
        CONVICTION_HIGH_TIER_MIN_SURFACES=2, CONVICTION_WATCH_TIER_MIN_SURFACES=1
    )
    res = cross_surface_conviction(_row(early=("chains", "momentum")), s)
    assert res.tier == "high"  # now high at 2


def test_early_lead_configurable():
    s = _settings(CONVICTION_EARLY_LEAD_MINUTES=60)
    res = cross_surface_conviction(_row(early=("chains",), lead=120.0), s)
    assert res.early_count == 1  # 120 >= 60


def test_surface_weights_override():
    s = _settings(CONVICTION_SURFACE_WEIGHTS={"chains": 3.0})
    res = cross_surface_conviction(_row(early=("chains", "momentum")), s)
    assert res.score == 4.0  # 3.0 (chains) + 1.0 (momentum default)
    assert res.early_count == 2  # count is unweighted


def test_contributing_in_surface_order():
    # velocity is last in SURFACE_LEAD_COLUMNS; chains first.
    res = cross_surface_conviction(_row(early=("velocity", "chains")), _settings())
    assert res.contributing == ("chains", "velocity")


def test_real_settings_defaults_smoke():
    from scout.config import Settings

    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="x",
        TELEGRAM_CHAT_ID="x",
        ANTHROPIC_API_KEY="x",
    )
    res = cross_surface_conviction(
        _row(early=("chains", "momentum", "slow_burn", "velocity")), s
    )
    assert res.tier == "high"
    assert TIER_ORDER == ("low", "watch", "high")
