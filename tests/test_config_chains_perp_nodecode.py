"""INF-01 CHAINS / PERP_SYMBOLS NoDecode boot-crash regression.

Config-only (imports scout.config, not the aiohttp dispatch module) so it runs
without the aiohttp import chain. Same pydantic-settings 2.13.x "complex field"
eager-JSON-decode trap the ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS fix (#430) closed:
`list[str]` env values are json.loads() by EnvSettingsSource BEFORE the
field_validator runs, so a comma-separated .env value raises SettingsError at
Settings() construction (boot crash-loop) unless the field is annotated NoDecode.

The pre-existing test_config.py CHAINS/PERP_SYMBOLS cases pass the value as a
constructor kwarg, which bypasses EnvSettingsSource entirely — so they never
exercised the env json.loads path where the crash lives. These use
monkeypatch.setenv to reproduce it.
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


# -------- CHAINS --------


def test_chains_env_comma_string_parses_to_list(monkeypatch):
    """INF-01 regression: the .env.example:14 form `CHAINS=solana,base,ethereum`
    must NOT raise SettingsError and must parse to a list (fails under plain
    list[str], passes with NoDecode)."""
    monkeypatch.setenv("CHAINS", "solana,base,ethereum")
    assert _settings().CHAINS == ["solana", "base", "ethereum"]


def test_chains_env_comma_string_strips_whitespace_and_blanks(monkeypatch):
    monkeypatch.setenv("CHAINS", " solana , , base ")
    assert _settings().CHAINS == ["solana", "base"]


def test_chains_env_json_array_string_parses_to_list(monkeypatch):
    """Back-compat: a JSON-array env value still parses to a list (parity with
    ALERT_UNIVERSE; the validator's `[`-branch handles it under NoDecode)."""
    monkeypatch.setenv("CHAINS", '["solana", "base"]')
    assert _settings().CHAINS == ["solana", "base"]


def test_chains_native_list_override_unchanged():
    """A native list passed to the constructor is preserved."""
    assert _settings(CHAINS=["base", "ethereum"]).CHAINS == ["base", "ethereum"]


def test_chains_default_unchanged():
    assert _settings().CHAINS == ["solana", "base", "ethereum"]


# -------- PERP_SYMBOLS --------


def test_perp_symbols_env_comma_string_parses_and_uppercases(monkeypatch):
    """INF-01 regression: the perp-spec documented comma form must NOT raise
    SettingsError; the validator's upper()/strip() normalization still applies."""
    monkeypatch.setenv("PERP_SYMBOLS", "btcusdt, ethusdt ,SOLUSDT")
    assert _settings().PERP_SYMBOLS == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_perp_symbols_env_json_array_string_parses_and_uppercases(monkeypatch):
    """Back-compat: a JSON-array env value still parses (and upper-normalizes),
    parity with ALERT_UNIVERSE via the validator's `[`-branch under NoDecode."""
    monkeypatch.setenv("PERP_SYMBOLS", '["btcusdt", "ethusdt"]')
    assert _settings().PERP_SYMBOLS == ["BTCUSDT", "ETHUSDT"]


def test_perp_symbols_native_list_override_unchanged():
    s = _settings(PERP_SYMBOLS=["btcusdt", " ethusdt ", "SOLUSDT"])
    assert s.PERP_SYMBOLS == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]


def test_perp_symbols_default_unchanged():
    assert _settings().PERP_SYMBOLS == []
