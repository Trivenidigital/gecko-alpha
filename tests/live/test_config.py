"""Tests for BL-055 LIVE_* settings (spec §4)."""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from scout.config import Settings


def _base_kwargs(**over):
    kw = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    kw.update(over)
    return kw


def test_live_mode_defaults_to_paper():
    s = Settings(**_base_kwargs())
    assert s.LIVE_MODE == "paper"


def test_live_mode_accepts_shadow_and_live():
    for mode in ("paper", "shadow", "live"):
        s = Settings(**_base_kwargs(LIVE_MODE=mode))
        assert s.LIVE_MODE == mode


def test_live_mode_rejects_unknown_value():
    with pytest.raises(ValidationError):
        Settings(**_base_kwargs(LIVE_MODE="yolo"))


def test_live_mode_case_sensitive_rejects_Live():
    """Pydantic `Literal` is case-sensitive; document/enforce that 'Live' fails."""
    with pytest.raises(ValidationError):
        Settings(**_base_kwargs(LIVE_MODE="Live"))


def test_live_sizing_defaults():
    s = Settings(**_base_kwargs())
    assert s.LIVE_TRADE_AMOUNT_USD == Decimal("100")
    assert s.LIVE_SIGNAL_SIZES == ""
    assert s.live_signal_sizes_map == {}


def test_live_signal_sizes_map_parses_csv():
    s = Settings(**_base_kwargs(
        LIVE_SIGNAL_SIZES="first_signal=50,gainers_early=75"
    ))
    assert s.live_signal_sizes_map == {
        "first_signal": Decimal("50"),
        "gainers_early": Decimal("75"),
    }


def test_live_signal_sizes_map_rejects_malformed():
    s = Settings(**_base_kwargs(LIVE_SIGNAL_SIZES="broken_no_equals"))
    with pytest.raises(ValueError, match="malformed"):
        _ = s.live_signal_sizes_map


def test_live_signal_allowlist_set_lowercased_and_trimmed():
    s = Settings(**_base_kwargs(
        LIVE_SIGNAL_ALLOWLIST=" First_Signal , gainers_early "
    ))
    assert s.live_signal_allowlist_set == frozenset(
        {"first_signal", "gainers_early"}
    )


def test_live_risk_gate_defaults():
    s = Settings(**_base_kwargs())
    assert s.LIVE_SLIPPAGE_BPS_CAP == 50
    assert s.LIVE_DEPTH_HEALTH_MULTIPLIER == Decimal("3")
    assert s.LIVE_DAILY_LOSS_CAP_USD == Decimal("50")
    assert s.LIVE_MAX_EXPOSURE_USD == Decimal("500")
    assert s.LIVE_MAX_OPEN_POSITIONS == 5


def test_settings_extra_forbid_rejects_typo():
    """Spec §4.5: extra='forbid' catches LIVE_* typos at startup."""
    with pytest.raises(ValidationError):
        Settings(**_base_kwargs(LIVE_MDOE="shadow"))  # typo: MDOE
