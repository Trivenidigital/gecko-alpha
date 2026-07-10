"""SIG-03 dispatch-quarantine config parsing (boot-crash regression).

Config-only (imports scout.config, not the aiohttp dispatch module) so it runs
without the aiohttp import chain. Covers the pydantic-settings "complex field"
eager-JSON-decode trap: `list[str]` env values are json.loads() by
EnvSettingsSource BEFORE the field_validator runs, so a comma-separated .env
value raises SettingsError at Settings() construction (boot crash-loop) unless
the field is annotated NoDecode. Mirrors
tests/test_config_alert_universe_filter.py.
"""

from __future__ import annotations

import pytest

from scout.config import Settings

_REQUIRED = {
    "TELEGRAM_BOT_TOKEN": "x",
    "TELEGRAM_CHAT_ID": "x",
    "ANTHROPIC_API_KEY": "x",
}


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


def test_default_is_two_negative_ev_lanes():
    """Default quarantines the two standing negative-EV lanes."""
    assert _settings().SIGNAL_DISPATCH_QUARANTINE == [
        "narrative_prediction",
        "tg_social",
    ]


def test_env_comma_string_parses_to_list(monkeypatch):
    """Regression: comma-separated .env value must NOT raise SettingsError and
    must parse to a list (fails under plain list[str]; passes with NoDecode)."""
    monkeypatch.setenv("SIGNAL_DISPATCH_QUARANTINE", "narrative_prediction,tg_social")
    s = _settings()
    assert s.SIGNAL_DISPATCH_QUARANTINE == ["narrative_prediction", "tg_social"]


def test_env_comma_string_strips_whitespace_and_blanks(monkeypatch):
    monkeypatch.setenv("SIGNAL_DISPATCH_QUARANTINE", " a , , b ")
    assert _settings().SIGNAL_DISPATCH_QUARANTINE == ["a", "b"]


def test_env_json_array_string_parses_to_list(monkeypatch):
    """Back-compat: a JSON-array env value still parses to a list."""
    monkeypatch.setenv("SIGNAL_DISPATCH_QUARANTINE", '["a", "b"]')
    assert _settings().SIGNAL_DISPATCH_QUARANTINE == ["a", "b"]


def test_empty_string_disables(monkeypatch):
    """Empty .env value => empty list => feature off (clean revert)."""
    monkeypatch.setenv("SIGNAL_DISPATCH_QUARANTINE", "")
    assert _settings().SIGNAL_DISPATCH_QUARANTINE == []


def test_native_list_override_unchanged():
    """A native list passed to the constructor is preserved."""
    s = _settings(SIGNAL_DISPATCH_QUARANTINE=["tg_social"])
    assert s.SIGNAL_DISPATCH_QUARANTINE == ["tg_social"]


def test_native_empty_list_override_disables():
    """An explicit empty list disables the feature."""
    assert _settings(SIGNAL_DISPATCH_QUARANTINE=[]).SIGNAL_DISPATCH_QUARANTINE == []
