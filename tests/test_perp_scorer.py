# tests/test_perp_scorer.py
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
    assert scorer_mod.SCORER_MAX_RAW < 203
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
