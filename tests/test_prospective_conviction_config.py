"""Config flags for the prospective sub-$30M conviction watchlist (V1)."""

import pytest
from pydantic import ValidationError


def test_prospective_conviction_flag_defaults(settings_factory):
    s = settings_factory()
    assert s.CONVICTION_PROSPECTIVE_ENABLED is True
    assert s.CONVICTION_WATCHLIST_MAX_MCAP == 30_000_000
    assert s.CONVICTION_WATCHLIST_MCAP_MAX_AGE_MINUTES == 1440
    assert s.CONVICTION_PROSPECTIVE_LOOKBACK_DAYS == 14
    assert s.CONVICTION_WATCHLIST_SNAPSHOT_RETENTION_DAYS == 90
    assert s.CONVICTION_WATCHLIST_SNAPSHOT_SLO_MINUTES == 180


def test_prospective_conviction_bounds_reject_invalid(settings_factory):
    with pytest.raises(ValidationError):
        settings_factory(CONVICTION_WATCHLIST_MAX_MCAP=-1)
    with pytest.raises(ValidationError):
        settings_factory(CONVICTION_PROSPECTIVE_LOOKBACK_DAYS=0)
    with pytest.raises(ValidationError):
        settings_factory(CONVICTION_WATCHLIST_SNAPSHOT_SLO_MINUTES=0)
