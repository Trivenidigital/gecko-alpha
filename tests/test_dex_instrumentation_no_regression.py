"""C7 — no-regression guard. Observe-only must not change scoring.

The scorer never reads DEX_INSTRUMENTATION_ENABLED, so its output is identical
regardless of the flag. (scorer.py and gate.py are untouched by this feature;
all capture is gated and lives in db.py / main.py / instrumentation/.)
"""

from scout.scorer import score


def test_scorer_output_independent_of_instrumentation_flag(token_factory, settings_factory):
    token = token_factory(
        market_cap_usd=50_000.0,
        liquidity_usd=20_000.0,
        volume_24h_usd=120_000.0,
        token_age_days=1.0,
    )
    off = settings_factory(DEX_INSTRUMENTATION_ENABLED=False)
    on = settings_factory(DEX_INSTRUMENTATION_ENABLED=True)
    assert score(token, off) == score(token, on)


def test_instrumentation_flag_defaults_off(settings_factory):
    # deploy must be byte-identical until the operator opts in
    assert settings_factory().DEX_INSTRUMENTATION_ENABLED is False
