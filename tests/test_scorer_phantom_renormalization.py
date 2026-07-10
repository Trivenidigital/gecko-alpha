"""SIG-02: scorer phantom-signal renormalization.

holder_growth (+25 raw) can only fire when MORALIS holder enrichment is
configured (holder_enricher.py:37). In prod MORALIS_API_KEY is empty, so
holder_growth is structurally unreachable — yet before SIG-02 it still sat in
the hardcoded 193-raw normalization divisor, capping realized scores.

These tests pin:
  (a) holder_growth's contribution AND its divisor weight are gated on the
      MORALIS capability;
  (b) the normalization divisor is DERIVED from the active-signal set;
  (c) snapshot — the same fixture scores today's value with the capability ON
      and a HIGHER value with it OFF (divisor shrinks 193 -> 168);
  (d) startup observability logs the active signal set + divisor.
"""

import structlog

from scout.scorer import (
    SCORER_MAX_RAW,
    active_scoring_signals,
    log_active_scoring_config,
    normalization_divisor,
    score,
)

MORALIS_ON = {"MORALIS_API_KEY": "test-moralis-key"}
MORALIS_OFF = {"MORALIS_API_KEY": ""}


# ---------------------------------------------------------------------------
# Divisor derivation (b)
# ---------------------------------------------------------------------------


def test_scorer_max_raw_is_derived_capability_on_maximum():
    # DERIVED from the weight table, not a magic literal:
    # 30+8+25+15+15+20+25+15+15+5+10+10 = 193 (holder_growth's 25 included).
    assert SCORER_MAX_RAW == 193


def test_divisor_full_when_capability_on(settings_factory):
    assert normalization_divisor(settings_factory(**MORALIS_ON)) == 193


def test_divisor_excludes_holder_growth_when_capability_off(settings_factory):
    # 193 - 25 (phantom holder_growth removed from the divisor) = 168.
    assert normalization_divisor(settings_factory(**MORALIS_OFF)) == 168


def test_active_signals_include_holder_growth_only_with_capability(settings_factory):
    assert "holder_growth" in active_scoring_signals(settings_factory(**MORALIS_ON))
    assert "holder_growth" not in active_scoring_signals(
        settings_factory(**MORALIS_OFF)
    )
    # Non-capability-gated signals are always active.
    assert "vol_liq_ratio" in active_scoring_signals(settings_factory(**MORALIS_OFF))


# ---------------------------------------------------------------------------
# holder_growth contribution gating (a)
# ---------------------------------------------------------------------------


def test_holder_growth_contribution_gated_on_capability(
    settings_factory, token_factory
):
    token = token_factory(
        liquidity_usd=20000.0,
        volume_24h_usd=1000.0,  # vol/liq = 0.05 -> vol_liq does NOT fire
        market_cap_usd=999999.0,  # outside cap tiers
        holder_growth_1h=25,  # would fire the +25 signal (>20)
        token_age_days=30.0,  # outside age curve
        chain="ethereum",  # no solana bonus
    )
    # Capability ON: holder_growth fires.
    _, on_signals = score(token, settings_factory(**MORALIS_ON))
    assert on_signals == ["holder_growth"]
    # Capability OFF: phantom cannot fire even with holder_growth_1h=25.
    _, off_signals = score(token, settings_factory(**MORALIS_OFF))
    assert "holder_growth" not in off_signals
    assert off_signals == []


# ---------------------------------------------------------------------------
# Snapshot: divisor renormalization raises OTHER signals when capability off (c)
# ---------------------------------------------------------------------------


def _divisor_isolation_token(token_factory):
    """Fires vol_liq_ratio (+30) + token_age (+15) only. holder_growth_1h=0 so
    holder_growth never fires in EITHER regime — this isolates the divisor
    effect from the contribution-gating effect."""
    return token_factory(
        liquidity_usd=20000.0,
        volume_24h_usd=120000.0,  # vol/liq = 6.0 > 5.0 -> vol_liq_ratio (+30)
        market_cap_usd=999999.0,  # outside cap tiers
        holder_growth_1h=0,  # holder_growth never fires in either regime
        token_age_days=1.0,  # 24h -> token_age peak band (+15)
        chain="ethereum",  # no solana bonus
    )


def test_snapshot_capability_on_pins_todays_behavior(settings_factory, token_factory):
    token = _divisor_isolation_token(token_factory)
    points, signals = score(token, settings_factory(**MORALIS_ON))
    assert sorted(signals) == ["token_age", "vol_liq_ratio"]
    # raw = 30 + 15 = 45; 2 signals (< 3 -> no co-occurrence multiplier).
    # divisor = 193 (capability on). int(45 * 100 / 193) = int(23.31) = 23.
    # This equals the pre-SIG-02 (hardcoded-193) score — the regression pin.
    assert points == 23


def test_snapshot_capability_off_scores_higher(settings_factory, token_factory):
    token = _divisor_isolation_token(token_factory)
    points, signals = score(token, settings_factory(**MORALIS_OFF))
    assert sorted(signals) == ["token_age", "vol_liq_ratio"]
    # Same 2 signals, raw = 45. divisor = 193 - 25 = 168 (holder_growth phantom
    # removed). int(45 * 100 / 168) = int(26.78) = 26 > 23 (capability-on).
    assert points == 26


def test_snapshot_off_strictly_higher_than_on(settings_factory, token_factory):
    token = _divisor_isolation_token(token_factory)
    on_points, _ = score(token, settings_factory(**MORALIS_ON))
    off_points, _ = score(token, settings_factory(**MORALIS_OFF))
    assert off_points > on_points  # phantom removal renormalizes upward


# ---------------------------------------------------------------------------
# Startup observability (d)
# ---------------------------------------------------------------------------


def test_log_active_scoring_config_emits_signals_and_divisor(settings_factory):
    with structlog.testing.capture_logs() as logs:
        log_active_scoring_config(settings_factory(**MORALIS_OFF))
    entries = [e for e in logs if e.get("event") == "scoring_config_active"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["normalization_divisor"] == 168
    assert entry["scorer_max_raw"] == 193
    assert "holder_growth" in entry["inactive_signals"]
    assert "holder_growth" not in entry["active_signals"]
    assert "vol_liq_ratio" in entry["active_signals"]
