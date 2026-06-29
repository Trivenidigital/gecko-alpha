"""C6 (logic) — fresh-but-empty alarm detection + flag-off no-op. Local-safe.

The alert *dispatch*/routing is covered separately under CI
(test_dex_instrumentation_watchdog_routing.py) since it imports the alerter
(aiohttp).
"""

from scout.instrumentation.watchdog import (
    _compute_alarms,
    check_dex_instrumentation_health,
)

_HEALTHY_COV = {"listed_dex": 3, "dex_resolution_health": 1.0, "dex_measurable_cohort_size": 3}
_EMPTY_COV = {"listed_dex": 0, "dex_resolution_health": 0.0, "dex_measurable_cohort_size": 0}


def test_entry_fresh_but_empty_alarms(settings_factory):
    settings = settings_factory(DEX_NONZERO_MCAP_FLOOR=0.9)
    stats = {
        "entry_total": 4, "entry_nonzero_rate": 0.0,
        "txns_total": 0, "txns_nonnull_rate": None,
        "map_total": 0, "map_resolved": 0,
    }
    alarms = _compute_alarms(stats, _EMPTY_COV, settings)
    assert any("entry_mcap fresh-but-empty" in a for a in alarms)


def test_txns_and_resolver_fresh_but_empty_alarm(settings_factory):
    settings = settings_factory(DEX_NONNULL_TXNS_FLOOR=0.5)
    stats = {
        "entry_total": 0, "entry_nonzero_rate": None,
        "txns_total": 10, "txns_nonnull_rate": 0.0,
        "map_total": 5, "map_resolved": 0,
    }
    alarms = _compute_alarms(stats, _EMPTY_COV, settings)
    assert any("txns_h1_buys fresh-but-empty" in a for a in alarms)
    assert any("resolver fresh-but-empty" in a for a in alarms)


def test_resolution_health_below_floor_alarms(settings_factory):
    settings = settings_factory(DEX_RESOLUTION_HEALTH_FLOOR=0.05)
    stats = {
        "entry_total": 0, "entry_nonzero_rate": None,
        "txns_total": 0, "txns_nonnull_rate": None,
        "map_total": 10, "map_resolved": 10,
    }
    cov = {"listed_dex": 100, "dex_resolution_health": 0.01, "dex_measurable_cohort_size": 1}
    alarms = _compute_alarms(stats, cov, settings)
    assert any("dex_resolution_health below floor" in a for a in alarms)


def test_healthy_data_no_alarm(settings_factory):
    settings = settings_factory()
    stats = {
        "entry_total": 4, "entry_nonzero_rate": 1.0,
        "txns_total": 10, "txns_nonnull_rate": 1.0,
        "map_total": 5, "map_resolved": 5,
    }
    assert _compute_alarms(stats, _HEALTHY_COV, settings) == []


def test_empty_tables_no_false_alarm(settings_factory):
    settings = settings_factory()
    stats = {
        "entry_total": 0, "entry_nonzero_rate": None,
        "txns_total": 0, "txns_nonnull_rate": None,
        "map_total": 0, "map_resolved": 0,
    }
    assert _compute_alarms(stats, _EMPTY_COV, settings) == []


async def test_check_health_flag_off_is_noop(settings_factory):
    settings = settings_factory(DEX_INSTRUMENTATION_ENABLED=False)
    alarms = await check_dex_instrumentation_health(db=None, session=None, settings=settings)
    assert alarms == []
