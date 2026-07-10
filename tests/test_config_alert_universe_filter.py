"""BL-NEW-ALERT-UNIVERSE-FILTER config parsing (S2-1 boot-crash regression).

Config-only (imports scout.config, not the aiohttp dispatch module) so it runs
without the aiohttp import chain. Covers the pydantic-settings 2.13.x
"complex field" eager-JSON-decode trap: `list[str]` env values are json.loads()
by EnvSettingsSource BEFORE the field_validator runs, so a comma-separated .env
value raises SettingsError at Settings() construction (boot crash-loop) unless
the field is annotated NoDecode.
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


def test_default_is_single_tokenized_pattern():
    """S2-2: the one default `-tokenized-` covers stock + etf offenders."""
    assert _settings().ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS == ["-tokenized-"]


@pytest.mark.parametrize(
    "slug",
    [
        "spy-bstocks-tokenized-stock",
        "qualcomm-bstocks-tokenized-stock",
        "western-digital-bstocks-tokenized-stock",
        "roundhill-memory-etf-bstocks-tokenized-stock",
        "invesco-qqq-etf-ondo-tokenized-etf",  # S2-2: `-tokenized-etf`, missed by old defaults
    ],
)
def test_default_pattern_covers_all_prod_offenders(slug):
    (pattern,) = _settings().ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS
    assert pattern in slug.lower()


def test_env_comma_string_parses_to_list(monkeypatch):
    """S2-1 regression: comma-separated .env value must NOT raise SettingsError
    and must parse to a list (fails under plain list[str]; passes with NoDecode)."""
    monkeypatch.setenv("ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS", "-a,-b")
    s = _settings()
    assert s.ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS == ["-a", "-b"]


def test_env_comma_string_strips_whitespace_and_blanks(monkeypatch):
    monkeypatch.setenv("ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS", " -a , , -b ")
    assert _settings().ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS == ["-a", "-b"]


def test_env_json_array_string_parses_to_list(monkeypatch):
    """Back-compat: a JSON-array env value still parses to a list."""
    monkeypatch.setenv("ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS", '["-a", "-b"]')
    assert _settings().ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS == ["-a", "-b"]


def test_native_list_override_unchanged():
    """A native list passed to the constructor is preserved."""
    s = _settings(ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS=["-wrapped-", "-tokenized-"])
    assert s.ALERT_UNIVERSE_EXCLUDE_ID_PATTERNS == ["-wrapped-", "-tokenized-"]
