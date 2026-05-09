"""BL-NEW-QUOTE-PAIR: scorer-side tests for stable_paired_liq signal.

The signal fires when token.quote_symbol matches a known stablecoin AND
liquidity_usd >= STABLE_PAIRED_LIQ_THRESHOLD_USD. Counts toward the
co-occurrence multiplier (intended behavior — stable-pair is real evidence).

Coverage matrix (per design + R3 reviewer):
- Happy path: USDC-paired ≥ threshold → signal fires
- Below threshold → does not fire
- Non-stable quote → does not fire
- None quote_symbol → does not fire
- Boundary (50_000.0 / 49_999.99 / 50_000.01) — fence-post check
- Case sensitivity — lowercase/mixed-case must NOT fire (catches API drift)
- All 8 stables parametrized — config-typo catcher
- Co-occurrence multiplier — score-delta vs naive-additive proves 1.15x fired
"""

from __future__ import annotations

import pytest

from scout.scorer import score


def _settings(settings_factory, **overrides):
    return settings_factory(**overrides)


def _make_token(token_factory, **overrides):
    """2-signal baseline tokens for boundary/parametrized tests."""
    defaults = dict(
        # vol_liq fires alone: vol_24h_usd / liquidity_usd >= 5
        volume_24h_usd=500_000.0,
        liquidity_usd=75_000.0,
        market_cap_usd=999_999.0,  # outside cap tier → no cap bonus
        token_age_days=30.0,  # outside age curve → no age bonus
        social_mentions_24h=0,
        holder_growth_1h=0,
        chain="ethereum",
    )
    defaults.update(overrides)
    return token_factory(**defaults)


# ----------------------------------------------------------------------
# Happy + negative paths
# ----------------------------------------------------------------------


def test_stable_paired_liq_fires_for_usdc_above_threshold(
    settings_factory, token_factory
):
    settings = _settings(settings_factory)
    token = _make_token(token_factory, quote_symbol="USDC", liquidity_usd=75_000.0)
    _, signals = score(token, settings)
    assert "stable_paired_liq" in signals


def test_stable_paired_liq_blocked_below_threshold(settings_factory, token_factory):
    settings = _settings(settings_factory)
    # liquidity 49_000 < threshold 50_000 — do NOT fire.
    # Need volume/liq >= 5 AND volume large enough to clear floor — but
    # liquidity_usd=49_000 is also below MIN_LIQUIDITY_USD=15_000? no, above.
    token = _make_token(
        token_factory,
        quote_symbol="USDC",
        liquidity_usd=49_000.0,
        volume_24h_usd=300_000.0,  # vol_liq still fires
    )
    _, signals = score(token, settings)
    assert "stable_paired_liq" not in signals


def test_stable_paired_liq_blocked_for_non_stable_quote(
    settings_factory, token_factory
):
    settings = _settings(settings_factory)
    token = _make_token(
        token_factory,
        quote_symbol="WSOL",
        liquidity_usd=200_000.0,
        volume_24h_usd=1_500_000.0,
    )
    _, signals = score(token, settings)
    assert "stable_paired_liq" not in signals


def test_stable_paired_liq_handles_none_quote_symbol(settings_factory, token_factory):
    settings = _settings(settings_factory)
    token = _make_token(
        token_factory,
        quote_symbol=None,
        liquidity_usd=200_000.0,
        volume_24h_usd=1_500_000.0,
    )
    _, signals = score(token, settings)
    assert "stable_paired_liq" not in signals


# ----------------------------------------------------------------------
# Boundary tests (R3 MUST-FIX)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "liquidity_usd, should_fire",
    [
        (50_000.0, True),  # exactly threshold — boundary inclusive
        (49_999.99, False),  # one cent under — boundary exclusive
        (50_000.01, True),  # one cent over — boundary inclusive
    ],
)
def test_stable_paired_liq_threshold_boundary(
    liquidity_usd, should_fire, settings_factory, token_factory
):
    settings = _settings(settings_factory)
    # Volume scaled to keep vol_liq firing across the range
    token = _make_token(
        token_factory,
        quote_symbol="USDC",
        liquidity_usd=liquidity_usd,
        volume_24h_usd=liquidity_usd * 8,
    )
    _, signals = score(token, settings)
    assert ("stable_paired_liq" in signals) is should_fire


# ----------------------------------------------------------------------
# All 8 stables parametrized (R3 NIT)
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "quote_symbol",
    ["USDC", "USDT", "DAI", "FDUSD", "USDe", "PYUSD", "RLUSD", "sUSDe"],
)
def test_stable_paired_liq_fires_for_all_listed_stables(
    quote_symbol, settings_factory, token_factory
):
    """A config typo in any of the 8 stables would silently break that path."""
    settings = _settings(settings_factory)
    token = _make_token(
        token_factory, quote_symbol=quote_symbol, liquidity_usd=75_000.0
    )
    _, signals = score(token, settings)
    assert (
        "stable_paired_liq" in signals
    ), f"stable_paired_liq did not fire for quote_symbol={quote_symbol!r}"


# ----------------------------------------------------------------------
# Case sensitivity (R3 MUST-FIX)
# ----------------------------------------------------------------------


@pytest.mark.parametrize("variant", ["usdc", "Usdc", "USDc", "uSdc"])
def test_stable_paired_liq_case_sensitivity(variant, settings_factory, token_factory):
    """DexScreener canonically returns uppercase. Decision: signal fires ONLY
    for exact-uppercase match. If API drifts to lowercase, this test fails
    and forces parser-side normalization rather than silent mis-firing."""
    settings = _settings(settings_factory)
    token = _make_token(token_factory, quote_symbol=variant, liquidity_usd=75_000.0)
    _, signals = score(token, settings)
    assert "stable_paired_liq" not in signals, (
        f"Unexpectedly fired for case variant {variant!r} — DexScreener case "
        f"convention may have changed; add normalization to from_dexscreener."
    )


# ----------------------------------------------------------------------
# Co-occurrence interaction (R3 MUST-FIX — score delta assertion)
# ----------------------------------------------------------------------


def test_stable_paired_liq_cooccurrence_score_delta(settings_factory, token_factory):
    """R3 MUST-FIX: assert measurable score uplift from 1.15x co-occurrence
    multiplier, not just signal-count bump.

    Recipe: 2 signals firing without stable-pair → adding stable-pair pushes
    to 3 signals → 1.15x multiplier kicks in. The post-multiplier score
    must exceed the naive +2 normalized direct bonus by the multiplier delta.
    """
    settings = _settings(settings_factory)

    # 2-signal token: vol_liq + holder_growth (no age, no cap, no chain bonus,
    # no stable). raw = 30 + 25 = 55, normalized = int(55 * 100/208) = 26.
    token_2sig = _make_token(
        token_factory,
        quote_symbol=None,  # no stable_paired_liq
        liquidity_usd=75_000.0,
        volume_24h_usd=500_000.0,  # vol_liq 6.66× > 5×
        holder_growth_1h=25,  # holder ≥ 20
    )
    score_2sig, signals_2sig = score(token_2sig, settings)
    assert len(signals_2sig) == 2, f"expected 2 signals, got {signals_2sig}"
    # raw=55, normalized=int(55*100/208)=26, no co-occurrence
    assert score_2sig == 26

    # 3-signal token: same + stable_paired_liq.
    # raw = 55 + 5 = 60, normalized = int(60*100/208) = 28, * 1.15 = int(32.2) = 32.
    token_3sig = _make_token(
        token_factory,
        quote_symbol="USDC",
        liquidity_usd=75_000.0,
        volume_24h_usd=500_000.0,
        holder_growth_1h=25,
    )
    score_3sig, signals_3sig = score(token_3sig, settings)
    assert len(signals_3sig) == 3, f"expected 3 signals, got {signals_3sig}"
    assert "stable_paired_liq" in signals_3sig
    # The 1.15x multiplier MUST fire — score must exceed naive +2 normalized.
    naive_additive_score = score_2sig + 2  # the bare-direct-bonus prediction
    assert score_3sig > naive_additive_score, (
        f"Co-occurrence multiplier did not fire: 2sig={score_2sig}, "
        f"3sig={score_3sig}, naive={naive_additive_score}"
    )
    # Concrete value lock to catch silent regression in normalization math.
    assert score_3sig == 32
