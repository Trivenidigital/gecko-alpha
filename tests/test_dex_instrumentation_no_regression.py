"""C7 — no-regression guard. Observe-only must not change scoring.

The scorer never reads DEX_INSTRUMENTATION_ENABLED, so its output is identical
regardless of the flag. (scorer.py and gate.py are untouched by this feature;
all capture is gated and lives in db.py / main.py / instrumentation/.)
"""

from scout.models import CandidateToken
from scout.scorer import score


def _gt_pool_with_strong_buy_pressure():
    # buys >> sells: if the scorer read GT counts, buy_pressure (+15) would fire.
    return {
        "attributes": {
            "name": "GT Token / SOL",
            "fdv_usd": "50000",
            "reserve_in_usd": "20000",
            "volume_usd": {"h24": "120000"},
            "transactions": {"h1": {"buys": 950, "sells": 50}},
        },
        "relationships": {"base_token": {"data": {"id": "solana_GTtokenMintAddr"}}},
    }


def test_geckoterminal_buy_pressure_does_not_enter_scorer(settings_factory):
    """BLOCKING-1 regression: GT counts must not change score (parser-input leak).

    A GT pool with 95% buy ratio would fire buy_pressure (+15) IF the scorer read
    GT counts. Because from_geckoterminal routes them to instrumentation-only
    fields, txns_h1_buys stays None and buy_pressure must NOT appear.
    """
    settings = settings_factory(DEX_INSTRUMENTATION_ENABLED=True)
    token = CandidateToken.from_geckoterminal(_gt_pool_with_strong_buy_pressure(), "solana")
    assert token.txns_h1_buys is None  # scorer-read field untouched by GT
    _points, signals = score(token, settings)
    assert "buy_pressure" not in signals


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
