"""Tests for deterministic flag computation (narrative + memecoin)."""

from __future__ import annotations

from scout.counter.flags import compute_memecoin_flags, compute_narrative_flags


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flag_names(flags):
    return {f.flag for f in flags}


def _get_flag(flags, name):
    return next(f for f in flags if f.flag == name)


# ---------------------------------------------------------------------------
# Narrative flags
# ---------------------------------------------------------------------------


class TestAlreadyPeaked:
    def test_high(self):
        flags = compute_narrative_flags(150, 20, 5000, 70, 80, 5, 5)
        f = _get_flag(flags, "already_peaked")
        assert f.severity == "high"

    def test_medium(self):
        flags = compute_narrative_flags(75, 20, 5000, 70, 80, 5, 5)
        f = _get_flag(flags, "already_peaked")
        assert f.severity == "medium"


class TestDeadProject:
    def test_high(self):
        flags = compute_narrative_flags(10, 0, 5000, 70, 80, 5, 5)
        f = _get_flag(flags, "dead_project")
        assert f.severity == "high"

    def test_medium(self):
        flags = compute_narrative_flags(10, 5, 5000, 70, 80, 5, 5)
        f = _get_flag(flags, "dead_project")
        assert f.severity == "medium"


class TestWeakCommunity:
    def test_high(self):
        flags = compute_narrative_flags(10, 20, 50, 70, 80, 5, 5)
        f = _get_flag(flags, "weak_community")
        assert f.severity == "high"

    def test_medium(self):
        flags = compute_narrative_flags(10, 20, 500, 70, 80, 5, 5)
        f = _get_flag(flags, "weak_community")
        assert f.severity == "medium"


class TestNegativeSentiment:
    def test_high(self):
        flags = compute_narrative_flags(10, 20, 5000, 30, 80, 5, 5)
        f = _get_flag(flags, "negative_sentiment")
        assert f.severity == "high"

    def test_medium(self):
        flags = compute_narrative_flags(10, 20, 5000, 45, 80, 5, 5)
        f = _get_flag(flags, "negative_sentiment")
        assert f.severity == "medium"


class TestVolumeDivergence:
    def test_high(self):
        flags = compute_narrative_flags(10, 20, 5000, 70, 80, -20, 15)
        f = _get_flag(flags, "volume_divergence")
        assert f.severity == "high"

    def test_no_flag_when_category_flat(self):
        flags = compute_narrative_flags(10, 20, 5000, 70, 80, -20, 5)
        assert "volume_divergence" not in _flag_names(flags)


class TestNarrativeMismatch:
    def test_high(self):
        flags = compute_narrative_flags(10, 20, 5000, 70, 30, 5, 5)
        f = _get_flag(flags, "narrative_mismatch")
        assert f.severity == "high"

    def test_medium(self):
        flags = compute_narrative_flags(10, 20, 5000, 70, 55, 5, 5)
        f = _get_flag(flags, "narrative_mismatch")
        assert f.severity == "medium"


def test_overvalued_vs_leaders_medium():
    flags = compute_narrative_flags(
        price_change_30d=10.0, commits_4w=50, reddit_subs=5000,
        sentiment_up_pct=60.0, narrative_fit_score=75,
        token_vol_change_24h=10.0, category_vol_growth_pct=15.0,
        market_cap=60e6, category_leader_mcap=100e6,
    )
    over = [f for f in flags if f.flag == "overvalued_vs_leaders"]
    assert len(over) == 1
    assert over[0].severity == "medium"


def test_overvalued_not_triggered():
    flags = compute_narrative_flags(
        price_change_30d=10.0, commits_4w=50, reddit_subs=5000,
        sentiment_up_pct=60.0, narrative_fit_score=75,
        token_vol_change_24h=10.0, category_vol_growth_pct=15.0,
        market_cap=20e6, category_leader_mcap=100e6,
    )
    over = [f for f in flags if f.flag == "overvalued_vs_leaders"]
    assert len(over) == 0


def test_volume_divergence_exact_boundary_no_trigger():
    """Exact -10 and +10 should NOT trigger (needs strictly < and >)."""
    flags = compute_narrative_flags(
        price_change_30d=10.0, commits_4w=50, reddit_subs=5000,
        sentiment_up_pct=60.0, narrative_fit_score=75,
        token_vol_change_24h=-10.0, category_vol_growth_pct=10.0,
    )
    div = [f for f in flags if f.flag == "volume_divergence"]
    assert len(div) == 0


def test_clean_token_no_flags():
    flags = compute_narrative_flags(
        price_change_30d=20,
        commits_4w=50,
        reddit_subs=5000,
        sentiment_up_pct=70,
        narrative_fit_score=80,
        token_vol_change_24h=10,
        category_vol_growth_pct=5,
    )
    assert flags == []


# ---------------------------------------------------------------------------
# Memecoin flags
# ---------------------------------------------------------------------------


class TestWashTrading:
    def test_high_above(self):
        flags = compute_memecoin_flags(0.98, 50000, 5, 10, 500, 5, False)
        f = _get_flag(flags, "wash_trading")
        assert f.severity == "high"

    def test_high_below(self):
        flags = compute_memecoin_flags(0.02, 50000, 5, 10, 500, 5, False)
        f = _get_flag(flags, "wash_trading")
        assert f.severity == "high"

    def test_medium_above(self):
        flags = compute_memecoin_flags(0.92, 50000, 5, 10, 500, 5, False)
        f = _get_flag(flags, "wash_trading")
        assert f.severity == "medium"

    def test_medium_below(self):
        flags = compute_memecoin_flags(0.08, 50000, 5, 10, 500, 5, False)
        f = _get_flag(flags, "wash_trading")
        assert f.severity == "medium"


class TestDeployerConcentration:
    def test_high(self):
        flags = compute_memecoin_flags(0.5, 50000, 5, 10, 500, 25, False)
        f = _get_flag(flags, "deployer_concentration")
        assert f.severity == "high"

    def test_medium(self):
        flags = compute_memecoin_flags(0.5, 50000, 5, 10, 500, 15, False)
        f = _get_flag(flags, "deployer_concentration")
        assert f.severity == "medium"


class TestLiquidityTrap:
    def test_high(self):
        flags = compute_memecoin_flags(0.5, 10000, 5, 10, 500, 5, False)
        f = _get_flag(flags, "liquidity_trap")
        assert f.severity == "high"

    def test_medium(self):
        flags = compute_memecoin_flags(0.5, 25000, 5, 10, 500, 5, False)
        f = _get_flag(flags, "liquidity_trap")
        assert f.severity == "medium"


class TestTokenTooNew:
    def test_high(self):
        flags = compute_memecoin_flags(0.5, 50000, 0.1, 10, 500, 5, False)
        f = _get_flag(flags, "token_too_new")
        assert f.severity == "high"

    def test_medium(self):
        flags = compute_memecoin_flags(0.5, 50000, 0.4, 10, 500, 5, False)
        f = _get_flag(flags, "token_too_new")
        assert f.severity == "medium"


class TestSuspiciousVolume:
    def test_high(self):
        flags = compute_memecoin_flags(0.5, 50000, 5, 60, 500, 5, False)
        f = _get_flag(flags, "suspicious_volume")
        assert f.severity == "high"

    def test_medium(self):
        flags = compute_memecoin_flags(0.5, 50000, 5, 30, 500, 5, False)
        f = _get_flag(flags, "suspicious_volume")
        assert f.severity == "medium"


class TestHoneypotRisk:
    def test_high(self):
        flags = compute_memecoin_flags(0.5, 50000, 5, 10, 500, 5, True)
        f = _get_flag(flags, "honeypot_risk")
        assert f.severity == "high"

    def test_no_flag_when_false(self):
        flags = compute_memecoin_flags(0.5, 50000, 5, 10, 500, 5, False)
        assert "honeypot_risk" not in _flag_names(flags)


class TestLowHolders:
    def test_high(self):
        flags = compute_memecoin_flags(0.5, 50000, 5, 10, 30, 5, False)
        f = _get_flag(flags, "low_holders")
        assert f.severity == "high"

    def test_medium(self):
        flags = compute_memecoin_flags(0.5, 50000, 5, 10, 150, 5, False)
        f = _get_flag(flags, "low_holders")
        assert f.severity == "medium"


def test_clean_memecoin_no_flags():
    flags = compute_memecoin_flags(
        buy_pressure=0.5,
        liquidity_usd=50000,
        token_age_days=5,
        vol_liq_ratio=10,
        holder_count=500,
        goplus_creator_pct=5,
        goplus_is_honeypot=False,
    )
    assert flags == []
