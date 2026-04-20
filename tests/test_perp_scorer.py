# tests/test_perp_scorer.py
import pytest
from datetime import datetime, timezone
from unittest.mock import patch
from scout import scorer as scorer_mod
from scout.scorer import score


def _tagged(token_factory, **extra):
    defaults = {
        "ticker": "DOGE",
        "liquidity_usd": 50_000,
        "perp_last_anomaly_at": datetime.now(timezone.utc),
        "perp_oi_spike_ratio": 4.5,
    }
    defaults.update(extra)
    return token_factory(**defaults)


def test_perp_signal_does_not_fire_when_flag_off(token_factory, settings_factory):
    settings = settings_factory(PERP_SCORING_ENABLED=False)
    token = _tagged(token_factory)
    points, signals = score(token, settings)
    assert "perp_anomaly" not in signals


def test_perp_signal_does_not_fire_when_denominator_not_ready(
    token_factory, settings_factory
):
    settings = settings_factory(PERP_SCORING_ENABLED=True)
    token = _tagged(token_factory)
    # SCORER_MAX_RAW ships at 183, so denominator-not-ready — signal must NOT fire.
    # Canary: if a future PR bumps SCORER_MAX_RAW without updating the guard,
    # this assert fails LOUDLY before we reach the behavioral check.
    assert scorer_mod.SCORER_MAX_RAW < 203
    assert scorer_mod._PERP_SCORING_DENOMINATOR_READY is False
    points, signals = score(token, settings)
    # Runtime guard: with SCORER_MAX_RAW < 203 we want points contributed by
    # the perp signal to be 0 and "perp_anomaly" NOT in signals_fired.
    assert "perp_anomaly" not in signals


def test_perp_signal_fires_when_both_flag_and_denominator_ready(
    token_factory,
    settings_factory,
):
    settings = settings_factory(PERP_SCORING_ENABLED=True, PERP_OI_SPIKE_RATIO=3.0)
    token = _tagged(token_factory)
    with (
        patch.object(scorer_mod, "SCORER_MAX_RAW", 203),
        patch.object(scorer_mod, "_PERP_SCORING_DENOMINATOR_READY", True),
    ):
        points, signals = score(token, settings)
    assert "perp_anomaly" in signals


def test_perp_signal_funding_flip_path(token_factory, settings_factory):
    settings = settings_factory(PERP_SCORING_ENABLED=True)
    token = _tagged(token_factory, perp_funding_flip=True, perp_oi_spike_ratio=None)
    with (
        patch.object(scorer_mod, "SCORER_MAX_RAW", 203),
        patch.object(scorer_mod, "_PERP_SCORING_DENOMINATOR_READY", True),
    ):
        points, signals = score(token, settings)
    assert "perp_anomaly" in signals


def test_perp_signal_skips_when_no_anomaly_timestamp(token_factory, settings_factory):
    settings = settings_factory(PERP_SCORING_ENABLED=True)
    token = _tagged(token_factory, perp_last_anomaly_at=None)
    with (
        patch.object(scorer_mod, "SCORER_MAX_RAW", 203),
        patch.object(scorer_mod, "_PERP_SCORING_DENOMINATOR_READY", True),
    ):
        points, signals = score(token, settings)
    assert "perp_anomaly" not in signals


@pytest.mark.parametrize(
    "perp_scoring_enabled, max_raw, ready, should_fire",
    [
        # SCORING=True but SCORER_MAX_RAW=202: denominator guard closed
        (True, 202, False, False),
        # SCORING=True + SCORER_MAX_RAW=203: guard open — signal fires
        (True, 203, True, True),
        # SCORING=False: must NOT fire regardless of denominator
        (False, 203, True, False),
        # SCORING=True + guard ready, but no anomaly timestamp (token has None)
        (True, 203, True, None),  # None means: use token with no anomaly
    ],
)
def test_perp_signal_flag_matrix(
    token_factory,
    settings_factory,
    perp_scoring_enabled,
    max_raw,
    ready,
    should_fire,
):
    settings = settings_factory(PERP_SCORING_ENABLED=perp_scoring_enabled)
    if should_fire is None:
        # Use a token with no anomaly data to test the no-fire path
        token = _tagged(
            token_factory, perp_last_anomaly_at=None, perp_oi_spike_ratio=None
        )
        expected_fire = False
    else:
        token = _tagged(token_factory)
        expected_fire = should_fire
    with (
        patch.object(scorer_mod, "SCORER_MAX_RAW", max_raw),
        patch.object(scorer_mod, "_PERP_SCORING_DENOMINATOR_READY", ready),
    ):
        _, signals = score(token, settings)
    if expected_fire:
        assert "perp_anomaly" in signals, f"Expected signal to fire: {signals}"
    else:
        assert "perp_anomaly" not in signals, f"Expected signal NOT to fire: {signals}"
