"""SIG-10 trust-weighted paper sizing -- resolver + config unit tests.

These are import-light (no aiohttp) so they run on Windows as well as CI; the
engine-integration coverage lives in test_trading_engine.py (aiohttp-bound,
Linux/CI only -- see that module's SIG-10 section).
"""

import json

import pytest

from scout.config import Settings
from scout.trading import trust_sizing


def _settings(**overrides):
    base = dict(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
    )
    base.update(overrides)
    return Settings(**base)


def _write_registry(tmp_path, entries):
    """Write a minimal signal-trust registry with the given (type, state) rows."""
    doc = {
        "schema_version": "signal_trust_registry.v1",
        "entries": [{"signal_type": st, "maturity_state": ms} for st, ms in entries],
    }
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# maturity_state -> tier mapping
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state,expected_tier",
    [
        ("trusted_experimental", "trusted"),
        ("context_only", "experimental"),
        ("data_insufficient", "experimental"),
        ("quarantined", "non_tradable"),
        ("retire_candidate", "non_tradable"),
    ],
)
def test_maturity_state_to_tier(tmp_path, state, expected_tier):
    path = _write_registry(tmp_path, [("sig", state)])
    tier, known = trust_sizing.resolve_trust_tier("sig", registry_path=path)
    assert (tier, known) == (expected_tier, True)


def test_unlisted_maturity_state_falls_back_to_experimental(tmp_path):
    path = _write_registry(tmp_path, [("sig", "some_new_state")])
    tier, known = trust_sizing.resolve_trust_tier("sig", registry_path=path)
    # present in registry (known) but state not enumerated -> experimental
    assert (tier, known) == ("experimental", True)


def test_unknown_signal_type_defaults_experimental(tmp_path):
    path = _write_registry(tmp_path, [("sig", "trusted_experimental")])
    tier, known = trust_sizing.resolve_trust_tier("absent", registry_path=path)
    assert (tier, known) == ("experimental", False)


def test_missing_registry_file_is_fail_soft(tmp_path):
    tier, known = trust_sizing.resolve_trust_tier(
        "sig", registry_path=tmp_path / "does_not_exist.json"
    )
    assert (tier, known) == ("experimental", False)


# --------------------------------------------------------------------------
# resolve_paper_trust_size: tier -> multiplier (incl. 0.0)
# --------------------------------------------------------------------------


def test_resolve_size_trusted_full(tmp_path):
    path = _write_registry(tmp_path, [("sig", "trusted_experimental")])
    tier, mult = trust_sizing.resolve_paper_trust_size(
        "sig", _settings(), registry_path=path
    )
    assert tier == "trusted"
    assert mult == 1.0


def test_resolve_size_experimental_half(tmp_path):
    path = _write_registry(tmp_path, [("sig", "context_only")])
    tier, mult = trust_sizing.resolve_paper_trust_size(
        "sig", _settings(), registry_path=path
    )
    assert tier == "experimental"
    assert mult == 0.5


def test_resolve_size_non_tradable_zero(tmp_path):
    path = _write_registry(tmp_path, [("sig", "quarantined")])
    tier, mult = trust_sizing.resolve_paper_trust_size(
        "sig", _settings(), registry_path=path
    )
    assert tier == "non_tradable"
    assert mult == 0.0


def test_resolve_size_unknown_signal_uses_experimental_multiplier(tmp_path):
    path = _write_registry(tmp_path, [("other", "trusted_experimental")])
    tier, mult = trust_sizing.resolve_paper_trust_size(
        "absent", _settings(), registry_path=path
    )
    assert tier == "experimental"
    assert mult == 0.5


def test_resolve_size_honors_custom_multiplier_map(tmp_path):
    path = _write_registry(tmp_path, [("sig", "trusted_experimental")])
    settings = _settings(
        PAPER_TRUST_SIZE_MULTIPLIERS="trusted=2.0,experimental=0.25,non_tradable=0.0"
    )
    tier, mult = trust_sizing.resolve_paper_trust_size(
        "sig", settings, registry_path=path
    )
    assert tier == "trusted"
    assert mult == 2.0


# --------------------------------------------------------------------------
# Real committed registry (docs/superpowers/registries/...)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "signal_type,expected_tier",
    [
        ("volume_spike", "trusted"),  # trusted_experimental
        ("chain_completed", "trusted"),  # trusted_experimental
        ("narrative_prediction", "experimental"),  # data_insufficient
        ("first_signal", "experimental"),  # data_insufficient
        ("tg", "experimental"),  # context_only
        ("x", "experimental"),  # context_only
    ],
)
def test_real_registry_tiers(signal_type, expected_tier):
    tier, known = trust_sizing.resolve_trust_tier(signal_type)
    assert (tier, known) == (expected_tier, True)


# --------------------------------------------------------------------------
# Config parsing: paper_trust_size_multipliers_map
# --------------------------------------------------------------------------


def test_config_multipliers_default_map():
    assert _settings().paper_trust_size_multipliers_map == {
        "trusted": 1.0,
        "experimental": 0.5,
        "non_tradable": 0.0,
    }


def test_config_multipliers_empty_string():
    assert (
        _settings(PAPER_TRUST_SIZE_MULTIPLIERS="").paper_trust_size_multipliers_map
        == {}
    )


def test_config_multipliers_malformed_raises():
    with pytest.raises(ValueError):
        _settings(
            PAPER_TRUST_SIZE_MULTIPLIERS="trusted"
        ).paper_trust_size_multipliers_map


def test_config_multipliers_negative_raises():
    with pytest.raises(ValueError):
        _settings(
            PAPER_TRUST_SIZE_MULTIPLIERS="trusted=-1.0"
        ).paper_trust_size_multipliers_map


def test_flag_defaults_off():
    assert _settings().PAPER_TRUST_SIZING_ENABLED is False
