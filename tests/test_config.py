"""Tests for scout.config module."""

from pathlib import Path

import pytest

from scout.config import Settings


def test_settings_loads_defaults():
    s = Settings(
        TELEGRAM_BOT_TOKEN="test-token",
        TELEGRAM_CHAT_ID="test-chat",
        ANTHROPIC_API_KEY="test-key",
        HELIUS_API_KEY="",
        _env_file=None,
    )
    assert s.SCAN_INTERVAL_SECONDS == 60
    assert s.MIN_SCORE == 60
    assert s.CONVICTION_THRESHOLD == 70
    assert s.QUANT_WEIGHT == 0.6
    assert s.NARRATIVE_WEIGHT == 0.4
    assert s.MIN_MARKET_CAP == 10_000
    assert s.MAX_MARKET_CAP == 500_000
    assert s.MAX_TOKEN_AGE_DAYS == 7
    assert s.MIN_LIQUIDITY_USD == 15_000
    assert s.MIN_VOL_LIQ_RATIO == 5.0
    assert s.CHAINS == ["solana", "base", "ethereum"]
    assert s.MIROFISH_URL == "http://localhost:5001"
    assert s.MIROFISH_TIMEOUT_SEC == 180
    assert s.MAX_MIROFISH_JOBS_PER_DAY == 50
    assert s.DB_PATH == Path("scout.db")
    assert isinstance(s.DB_PATH, Path)
    assert s.HELIUS_API_KEY == ""
    assert s.MORALIS_API_KEY == ""
    assert s.DISCORD_WEBHOOK_URL == ""


def test_settings_chains_parsing_from_string():
    s = Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        CHAINS="solana,polygon",
    )
    assert s.CHAINS == ["solana", "polygon"]


def test_settings_chains_parsing_from_list():
    s = Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        CHAINS=["base", "ethereum"],
    )
    assert s.CHAINS == ["base", "ethereum"]


def test_settings_custom_overrides():
    s = Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        MIN_SCORE=40,
        CONVICTION_THRESHOLD=80,
        SCAN_INTERVAL_SECONDS=30,
        MAX_MIROFISH_JOBS_PER_DAY=100,
    )
    assert s.MIN_SCORE == 40
    assert s.CONVICTION_THRESHOLD == 80
    assert s.SCAN_INTERVAL_SECONDS == 30
    assert s.MAX_MIROFISH_JOBS_PER_DAY == 100


def test_coingecko_config_defaults():
    """CoinGecko config knobs have correct defaults."""
    s = Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    assert s.MOMENTUM_RATIO_THRESHOLD == 0.6
    assert s.MIN_VOL_ACCEL_RATIO == 5.0
    assert s.COINGECKO_MIN_REQUEST_INTERVAL_SEC == 0.75
    assert s.COINGECKO_REQUEST_JITTER_SEC == 0.25
    assert s.COINGECKO_429_COOLDOWN_SEC == 120.0


def test_ingest_watchdog_config_defaults():
    s = Settings(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    assert s.INGEST_WATCHDOG_ENABLED is True
    assert s.INGEST_STARVATION_THRESHOLD_CYCLES == 5


def test_ingest_watchdog_threshold_validator():
    with pytest.raises(ValueError, match="INGEST_STARVATION_THRESHOLD_CYCLES"):
        Settings(
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            INGEST_STARVATION_THRESHOLD_CYCLES=0,
        )


def test_settings_weight_sum_validation():
    with pytest.raises(ValueError, match="must sum to 1.0"):
        Settings(
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            QUANT_WEIGHT=0.7,
            NARRATIVE_WEIGHT=0.4,
        )


def test_feedback_loop_defaults(monkeypatch):
    """All feedback-loop settings have sensible defaults per spec §8."""
    monkeypatch.delenv("FEEDBACK_SUPPRESSION_MIN_TRADES", raising=False)
    monkeypatch.delenv("FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT", raising=False)
    monkeypatch.delenv("FEEDBACK_PAROLE_DAYS", raising=False)
    monkeypatch.delenv("FEEDBACK_PAROLE_RETEST_TRADES", raising=False)
    monkeypatch.delenv("FEEDBACK_MIN_LEADERBOARD_TRADES", raising=False)
    monkeypatch.delenv("FEEDBACK_MISSED_WINNER_MIN_PCT", raising=False)
    monkeypatch.delenv("FEEDBACK_MISSED_WINNER_MIN_MCAP", raising=False)
    monkeypatch.delenv("FEEDBACK_MISSED_WINNER_WINDOW_MIN", raising=False)
    monkeypatch.delenv("FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN", raising=False)
    monkeypatch.delenv("FEEDBACK_WEEKLY_DIGEST_WEEKDAY", raising=False)
    monkeypatch.delenv("FEEDBACK_WEEKLY_DIGEST_HOUR", raising=False)
    monkeypatch.delenv("FEEDBACK_COMBO_REFRESH_HOUR", raising=False)
    monkeypatch.delenv("FEEDBACK_FALLBACK_ALERT_THRESHOLD", raising=False)
    monkeypatch.delenv("FEEDBACK_FALLBACK_ALERT_COOLDOWN_SEC", raising=False)
    monkeypatch.delenv("FEEDBACK_CHRONIC_FAILURE_THRESHOLD", raising=False)

    from scout.config import Settings

    s = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
    )
    assert s.FEEDBACK_SUPPRESSION_MIN_TRADES == 20
    assert s.FEEDBACK_SUPPRESSION_WR_THRESHOLD_PCT == 30.0
    assert s.FEEDBACK_PAROLE_DAYS == 14
    assert s.FEEDBACK_PAROLE_RETEST_TRADES == 5
    assert s.FEEDBACK_MIN_LEADERBOARD_TRADES == 10
    assert s.FEEDBACK_MISSED_WINNER_MIN_PCT == 50.0
    assert s.FEEDBACK_MISSED_WINNER_MIN_MCAP == 5_000_000
    assert s.FEEDBACK_MISSED_WINNER_WINDOW_MIN == 30
    assert s.FEEDBACK_PIPELINE_GAP_THRESHOLD_MIN == 60
    assert s.FEEDBACK_WEEKLY_DIGEST_WEEKDAY == 6
    assert s.FEEDBACK_WEEKLY_DIGEST_HOUR == 9
    assert s.FEEDBACK_COMBO_REFRESH_HOUR == 3
    assert s.FEEDBACK_FALLBACK_ALERT_THRESHOLD == 5
    assert s.FEEDBACK_FALLBACK_ALERT_COOLDOWN_SEC == 900
    assert s.FEEDBACK_CHRONIC_FAILURE_THRESHOLD == 3


def test_parse_perp_symbols_normalizes_list_input():
    from scout.config import Settings

    s = Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        PERP_SYMBOLS=["btcusdt", " ethusdt ", "SOLUSDT"],
    )
    assert s.PERP_SYMBOLS == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_parse_perp_symbols_rejects_over_200_items_from_list():
    import pytest
    from pydantic import ValidationError
    from scout.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            TELEGRAM_BOT_TOKEN="test",
            TELEGRAM_CHAT_ID="test",
            ANTHROPIC_API_KEY="test",
            PERP_SYMBOLS=[f"SYM{i}" for i in range(201)],
        )


def test_bl061_ladder_config_defaults(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for var in (
        "PAPER_LADDER_LEG_1_PCT",
        "PAPER_LADDER_LEG_1_QTY_FRAC",
        "PAPER_LADDER_LEG_2_PCT",
        "PAPER_LADDER_LEG_2_QTY_FRAC",
        "PAPER_LADDER_TRAIL_PCT",
        "PAPER_LADDER_FLOOR_ARM_ON_LEG_1",
        "PAPER_SL_PCT",
    ):
        monkeypatch.delenv(var, raising=False)
    from scout.config import Settings

    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="x",
        TELEGRAM_CHAT_ID="1",
        ANTHROPIC_API_KEY="k",
    )
    assert s.PAPER_LADDER_LEG_1_PCT == 25.0
    assert s.PAPER_LADDER_LEG_1_QTY_FRAC == 0.30
    assert s.PAPER_LADDER_LEG_2_PCT == 50.0
    assert s.PAPER_LADDER_LEG_2_QTY_FRAC == 0.30
    assert s.PAPER_LADDER_TRAIL_PCT == 12.0
    assert s.PAPER_LADDER_FLOOR_ARM_ON_LEG_1 is True
    assert s.PAPER_SL_PCT == 15.0
    # BL-060 fields removed
    assert not hasattr(s, "PAPER_MIN_QUANT_SCORE")
    assert not hasattr(s, "PAPER_LIVE_ELIGIBLE_CAP")


def test_bl061_qty_frac_rejects_oversell(monkeypatch, tmp_path):
    """Fractions > 1.0 would oversell the position — must raise."""
    import pytest
    from pydantic import ValidationError

    monkeypatch.chdir(tmp_path)
    from scout.config import Settings

    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="x",
            TELEGRAM_CHAT_ID="1",
            ANTHROPIC_API_KEY="k",
            PAPER_LADDER_LEG_1_QTY_FRAC=1.5,
        )
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="x",
            TELEGRAM_CHAT_ID="1",
            ANTHROPIC_API_KEY="k",
            PAPER_LADDER_LEG_2_QTY_FRAC=0.0,
        )


def test_ladder_qty_fracs_reject_no_runner(monkeypatch, tmp_path):
    """leg_1 + leg_2 >= 1.0 leaves no runner slice — reject at settings load."""
    import pytest
    from pydantic import ValidationError

    monkeypatch.chdir(tmp_path)
    from scout.config import Settings

    with pytest.raises(ValidationError, match="must be < 1.0 to leave a runner slice"):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            PAPER_LADDER_LEG_1_QTY_FRAC=0.5,
            PAPER_LADDER_LEG_2_QTY_FRAC=0.5,
        )
    with pytest.raises(ValidationError, match="must be < 1.0 to leave a runner slice"):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            PAPER_LADDER_LEG_1_QTY_FRAC=0.6,
            PAPER_LADDER_LEG_2_QTY_FRAC=0.5,
        )


def test_ladder_qty_fracs_defaults_leave_runner(monkeypatch, tmp_path):
    """Defaults 0.30 + 0.30 = 0.60 → runner = 0.40, valid."""
    monkeypatch.chdir(tmp_path)
    from scout.config import Settings

    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    total = s.PAPER_LADDER_LEG_1_QTY_FRAC + s.PAPER_LADDER_LEG_2_QTY_FRAC
    assert total < 1.0


# ---------------------------------------------------------------------------
# BL-062 signal-stacking + peak-fade validators
# ---------------------------------------------------------------------------


def test_first_signal_min_signal_count_default_is_two(tmp_path, monkeypatch):
    from scout.config import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FIRST_SIGNAL_MIN_SIGNAL_COUNT", raising=False)
    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    assert s.FIRST_SIGNAL_MIN_SIGNAL_COUNT == 2


def test_first_signal_min_signal_count_rejects_zero(monkeypatch):
    from pydantic import ValidationError
    from scout.config import Settings

    monkeypatch.setenv("FIRST_SIGNAL_MIN_SIGNAL_COUNT", "0")
    with pytest.raises(ValidationError, match="FIRST_SIGNAL_MIN_SIGNAL_COUNT"):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
        )


def test_peak_fade_enabled_default_true(tmp_path, monkeypatch):
    from scout.config import Settings

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PEAK_FADE_ENABLED", raising=False)
    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    assert s.PEAK_FADE_ENABLED is True


def test_peak_fade_min_peak_pct_rejects_zero(monkeypatch):
    from pydantic import ValidationError
    from scout.config import Settings

    monkeypatch.setenv("PEAK_FADE_MIN_PEAK_PCT", "0")
    with pytest.raises(ValidationError, match="PEAK_FADE_MIN_PEAK_PCT"):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
        )


def test_peak_fade_min_peak_pct_rejects_negative(monkeypatch):
    from pydantic import ValidationError
    from scout.config import Settings

    monkeypatch.setenv("PEAK_FADE_MIN_PEAK_PCT", "-5")
    with pytest.raises(ValidationError, match="PEAK_FADE_MIN_PEAK_PCT"):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
        )


def test_peak_fade_retrace_ratio_rejects_one(monkeypatch):
    from pydantic import ValidationError
    from scout.config import Settings

    monkeypatch.setenv("PEAK_FADE_RETRACE_RATIO", "1.0")
    with pytest.raises(ValidationError, match="PEAK_FADE_RETRACE_RATIO"):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
        )


def test_peak_fade_retrace_ratio_rejects_zero(monkeypatch):
    from pydantic import ValidationError
    from scout.config import Settings

    monkeypatch.setenv("PEAK_FADE_RETRACE_RATIO", "0")
    with pytest.raises(ValidationError, match="PEAK_FADE_RETRACE_RATIO"):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
        )


def test_peak_fade_retrace_ratio_accepts_half(monkeypatch):
    from scout.config import Settings

    monkeypatch.setenv("PEAK_FADE_RETRACE_RATIO", "0.5")
    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    assert s.PEAK_FADE_RETRACE_RATIO == 0.5


def test_bl063_moonshot_defaults(monkeypatch, tmp_path):
    """BL-063 moonshot config defaults — flag off, threshold 40, trail 30."""
    monkeypatch.chdir(tmp_path)
    for var in (
        "PAPER_MOONSHOT_ENABLED",
        "PAPER_MOONSHOT_THRESHOLD_PCT",
        "PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT",
    ):
        monkeypatch.delenv(var, raising=False)
    from scout.config import Settings

    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    assert s.PAPER_MOONSHOT_ENABLED is False
    assert s.PAPER_MOONSHOT_THRESHOLD_PCT == 40.0
    assert s.PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT == 30.0


def test_bl063_moonshot_rejects_non_positive_threshold():
    """THRESHOLD_PCT <= 0 would arm every trade at open."""
    from pydantic import ValidationError
    from scout.config import Settings

    with pytest.raises(
        ValidationError, match="PAPER_MOONSHOT_THRESHOLD_PCT must be > 0"
    ):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            PAPER_MOONSHOT_THRESHOLD_PCT=0.0,
        )


def test_bl063_moonshot_rejects_drawdown_out_of_range():
    """Trail drawdown must be in (0, 100); 100+ silently disables trailing."""
    from pydantic import ValidationError
    from scout.config import Settings

    with pytest.raises(
        ValidationError,
        match=r"PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT must be in \(0, 100\)",
    ):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=100.0,
        )


def test_bl063_moonshot_rejects_trail_narrower_than_ladder():
    """Cross-field guard: moonshot trail must WIDEN, never tighten."""
    from pydantic import ValidationError
    from scout.config import Settings

    with pytest.raises(ValidationError, match="moonshot widens the trail"):
        Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            PAPER_LADDER_TRAIL_PCT=25.0,
            PAPER_MOONSHOT_TRAIL_DRAWDOWN_PCT=20.0,
        )


# ---------------------------------------------------------------------------
# BL-064 channel-reload — validator (PR #73 PR-review HV-1)
# ---------------------------------------------------------------------------


def test_channel_reload_interval_allows_zero_as_explicit_disable():
    """PR-review HV-1: validator accepts 0 as the explicit opt-out
    (was rejected by `if v < 60` in the original validator before the
    BL-064 reload PR amended it to `if v != 0 and v < 60`)."""
    from scout.config import Settings

    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=0,
    )
    assert s.TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC == 0


def test_channel_reload_interval_rejects_below_60_and_nonzero():
    """PR-review HV-1: anti-thrash guard — 1-59 still rejected so an
    operator can't accidentally hot-loop the DB."""
    from pydantic import ValidationError
    from scout.config import Settings

    for bad_value in (1, 30, 59, -5):
        with pytest.raises(
            ValidationError,
            match="must be >= 60",
        ):
            Settings(
                _env_file=None,
                TELEGRAM_BOT_TOKEN="t",
                TELEGRAM_CHAT_ID="c",
                ANTHROPIC_API_KEY="k",
                TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=bad_value,
            )


def test_channel_reload_interval_accepts_60_and_above():
    """PR-review HV-1: 60+ accepted; default 300 accepted."""
    from scout.config import Settings

    for good_value in (60, 300, 3600):
        s = Settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
            TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=good_value,
        )
        assert s.TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC == good_value
