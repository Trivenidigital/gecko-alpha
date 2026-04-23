"""Spec §10.1 error taxonomy — typed exceptions for live pipeline."""

from scout.live.exceptions import (
    DepthInsufficient,
    KillSwitchActive,
    LiveError,
    RateLimitError,
    VenueNotResolved,
    VenueTransientError,
)


def test_exception_hierarchy():
    # All live exceptions derive from LiveError.
    assert issubclass(VenueTransientError, LiveError)
    assert issubclass(VenueNotResolved, LiveError)
    assert issubclass(DepthInsufficient, LiveError)
    assert issubclass(RateLimitError, LiveError)
    assert issubclass(KillSwitchActive, LiveError)
    # RateLimitError is a VenueTransientError (429 / weight>95%).
    assert issubclass(RateLimitError, VenueTransientError)
    # VenueNotResolved is NOT transient (do not retry).
    assert not issubclass(VenueNotResolved, VenueTransientError)


def test_instances_carry_messages():
    e = VenueTransientError("binance 502 — 3x retry exhausted")
    assert "502" in str(e)
    assert isinstance(e, LiveError)
