"""Tests for BL-051 config settings."""

from scout.config import Settings


def test_min_boost_total_amount_default(settings_factory):
    s = settings_factory()
    assert s.MIN_BOOST_TOTAL_AMOUNT == 500.0


def test_dexscreener_top_boosts_poll_every_cycles_default(settings_factory):
    s = settings_factory()
    assert s.DEXSCREENER_TOP_BOOSTS_POLL_EVERY_CYCLES == 1


def test_min_boost_total_amount_override(settings_factory):
    s = settings_factory(MIN_BOOST_TOTAL_AMOUNT=1000.0)
    assert s.MIN_BOOST_TOTAL_AMOUNT == 1000.0
