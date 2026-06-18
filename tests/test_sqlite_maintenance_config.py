"""Config flags for durable SQLite maintenance (P0 Part B)."""

import pytest
from pydantic import ValidationError


def test_sqlite_maintenance_flag_defaults(settings_factory):
    s = settings_factory()
    assert s.SQLITE_WAL_CHECKPOINT_ENABLED is True
    assert s.SQLITE_WAL_CHECKPOINT_THRESHOLD_BYTES == 100_000_000
    assert s.SQLITE_WAL_CHECKPOINT_BUSY_ALERT_THRESHOLD == 3
    assert s.SQLITE_INCREMENTAL_VACUUM_ENABLED is True
    assert s.SQLITE_INCREMENTAL_VACUUM_FREELIST_THRESHOLD == 50_000
    assert s.SQLITE_INCREMENTAL_VACUUM_MAX_PAGES == 200_000
    assert s.SQLITE_STALE_READER_WATCHDOG_ENABLED is True
    assert s.SQLITE_STALE_READER_MAX_AGE_HOURS == 6.0
    assert s.SQLITE_STALE_READER_ALERT_ENABLED is True
    assert s.SQLITE_EXPECTED_SERVICE_UNITS == [
        "gecko-pipeline.service",
        "gecko-dashboard.service",
    ]


def test_sqlite_maintenance_numeric_bounds_reject_invalid(settings_factory):
    with pytest.raises(ValidationError):
        settings_factory(SQLITE_INCREMENTAL_VACUUM_FREELIST_THRESHOLD=-1)
    with pytest.raises(ValidationError):
        settings_factory(SQLITE_WAL_CHECKPOINT_THRESHOLD_BYTES=-1)
    with pytest.raises(ValidationError):
        settings_factory(SQLITE_STALE_READER_MAX_AGE_HOURS=0)
    with pytest.raises(ValidationError):
        settings_factory(SQLITE_WAL_CHECKPOINT_BUSY_ALERT_THRESHOLD=0)
