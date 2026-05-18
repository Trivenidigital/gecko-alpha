"""Tests for scout/config_alert.py — curl-direct Telegram alert on
settings_validation_failed events (BL-NEW-SETTINGS-VALIDATION-ALERT, cycle 14).

Stdlib-only mocks (no aiohttp/aioresponses) since the helper is synchronous
urllib.request-based.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest


# Plan R2 I5 fold: autouse fixture clears every env var the helper consults
# so tests are deterministic regardless of the developer's local environment.
@pytest.fixture(autouse=True)
def _clean_telegram_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("SETTINGS_VALIDATION_ALERT_STATE_DIR", raising=False)
    monkeypatch.delenv("GECKO_ENV_FILE", raising=False)


def _seed_env_file(env_file: Path, **overrides):
    """Write a .env file with TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID. Override via kwargs."""
    token = overrides.get("token", "real_looking_token_format")
    chat = overrides.get("chat", "12345")
    env_file.write_text(
        f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat}\n", encoding="utf-8"
    )


def _set_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """Point state-dir + env-file at tmp_path; return (state_dir, env_file)."""
    state_dir = tmp_path / "state"
    env_file = tmp_path / ".env"
    monkeypatch.setenv("SETTINGS_VALIDATION_ALERT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("GECKO_ENV_FILE", str(env_file))
    return state_dir, env_file


def _mock_urlopen_200():
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.__enter__ = lambda self: mock_resp
    mock_resp.__exit__ = lambda self, *a: None
    return mock_resp


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_send_alert_returns_sent_on_http_200(monkeypatch, tmp_path):
    from scout.config_alert import _send_validation_alert_best_effort
    state_dir, env_file = _set_paths(monkeypatch, tmp_path)
    _seed_env_file(env_file)
    with patch("urllib.request.urlopen") as urlopen_mock:
        urlopen_mock.return_value = _mock_urlopen_200()
        result = _send_validation_alert_best_effort("BOOM: BL060_VOLUME_DAYS_BACK invalid")
    assert result == "sent"
    ack_file = state_dir / "last_alerted_hash"
    assert ack_file.exists()
    assert len(ack_file.read_text(encoding="utf-8").strip()) == 64  # sha256 hex
    urlopen_mock.assert_called_once()
    # Verify payload shape
    req_arg = urlopen_mock.call_args[0][0]
    payload = json.loads(req_arg.data.decode("utf-8"))
    assert payload["chat_id"] == "12345"
    assert "⚠️ settings_validation_failed" in payload["text"]
    assert "BOOM" in payload["text"]
    # §12b compliance: no parse_mode field
    assert "parse_mode" not in payload


# ---------------------------------------------------------------------------
# No-creds / placeholder paths (R2 I6 fold: assert urlopen NOT called)
# ---------------------------------------------------------------------------


def test_send_alert_skipped_on_missing_token(monkeypatch, tmp_path):
    from scout.config_alert import _send_validation_alert_best_effort
    _set_paths(monkeypatch, tmp_path)
    # No .env file → no creds discoverable
    with patch("urllib.request.urlopen") as urlopen_mock:
        result = _send_validation_alert_best_effort("error")
    assert result == "skipped:no_creds"
    urlopen_mock.assert_not_called()


def test_send_alert_skipped_on_placeholder_token(monkeypatch, tmp_path):
    from scout.config_alert import _send_validation_alert_best_effort
    _, env_file = _set_paths(monkeypatch, tmp_path)
    _seed_env_file(env_file, token="placeholder")
    with patch("urllib.request.urlopen") as urlopen_mock:
        result = _send_validation_alert_best_effort("error")
    assert result == "skipped:no_creds"
    urlopen_mock.assert_not_called()


def test_send_alert_skipped_on_missing_chat_id(monkeypatch, tmp_path):
    from scout.config_alert import _send_validation_alert_best_effort
    _, env_file = _set_paths(monkeypatch, tmp_path)
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=real_token\n", encoding="utf-8"
    )  # token only
    with patch("urllib.request.urlopen") as urlopen_mock:
        result = _send_validation_alert_best_effort("error")
    assert result == "skipped:no_creds"
    urlopen_mock.assert_not_called()


def test_send_alert_skipped_on_placeholder_chat_id(monkeypatch, tmp_path):
    from scout.config_alert import _send_validation_alert_best_effort
    _, env_file = _set_paths(monkeypatch, tmp_path)
    _seed_env_file(env_file, chat="placeholder")
    with patch("urllib.request.urlopen") as urlopen_mock:
        result = _send_validation_alert_best_effort("error")
    assert result == "skipped:no_creds"
    urlopen_mock.assert_not_called()


def test_send_alert_resolves_token_from_env_and_chat_from_envfile(
    monkeypatch, tmp_path
):
    """Plan R2 I1 fold: cross-source resolution — token from os.environ, chat from .env."""
    from scout.config_alert import _send_validation_alert_best_effort
    _, env_file = _set_paths(monkeypatch, tmp_path)
    env_file.write_text("TELEGRAM_CHAT_ID=98765\n", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "from_env_token")
    with patch("urllib.request.urlopen") as urlopen_mock:
        urlopen_mock.return_value = _mock_urlopen_200()
        result = _send_validation_alert_best_effort("error")
    assert result == "sent"
    # Confirm the URL contains the env-sourced token
    req_arg = urlopen_mock.call_args[0][0]
    assert "from_env_token" in req_arg.full_url
    payload = json.loads(req_arg.data.decode("utf-8"))
    assert payload["chat_id"] == "98765"


# ---------------------------------------------------------------------------
# Dedup
# ---------------------------------------------------------------------------


def test_send_alert_dedup_on_same_error_hash(monkeypatch, tmp_path):
    from scout.config_alert import _send_validation_alert_best_effort
    _, env_file = _set_paths(monkeypatch, tmp_path)
    _seed_env_file(env_file)
    with patch("urllib.request.urlopen") as urlopen_mock:
        urlopen_mock.return_value = _mock_urlopen_200()
        assert _send_validation_alert_best_effort("identical-error") == "sent"
        assert _send_validation_alert_best_effort("identical-error") == "skipped:dedup"
    assert urlopen_mock.call_count == 1


def test_send_alert_resends_on_different_error(monkeypatch, tmp_path):
    from scout.config_alert import _send_validation_alert_best_effort
    _, env_file = _set_paths(monkeypatch, tmp_path)
    _seed_env_file(env_file)
    with patch("urllib.request.urlopen") as urlopen_mock:
        urlopen_mock.return_value = _mock_urlopen_200()
        assert _send_validation_alert_best_effort("error-A") == "sent"
        assert _send_validation_alert_best_effort("error-B") == "sent"
    assert urlopen_mock.call_count == 2


# ---------------------------------------------------------------------------
# State-dir / failure paths
# ---------------------------------------------------------------------------


def test_send_alert_skipped_on_state_dir_unwritable(monkeypatch, tmp_path):
    from scout.config_alert import _send_validation_alert_best_effort
    sentinel = tmp_path / "sentinel-as-file"
    sentinel.write_text("not a directory")
    monkeypatch.setenv("SETTINGS_VALIDATION_ALERT_STATE_DIR", str(sentinel / "state"))
    env_file = tmp_path / ".env"
    monkeypatch.setenv("GECKO_ENV_FILE", str(env_file))
    _seed_env_file(env_file)
    with patch("urllib.request.urlopen") as urlopen_mock:
        result = _send_validation_alert_best_effort("error")
    assert result == "skipped:state_dir_unwritable"
    urlopen_mock.assert_not_called()


def test_send_alert_skipped_on_http_non_200(monkeypatch, tmp_path):
    from scout.config_alert import _send_validation_alert_best_effort
    state_dir, env_file = _set_paths(monkeypatch, tmp_path)
    _seed_env_file(env_file)
    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.__enter__ = lambda self: mock_resp
    mock_resp.__exit__ = lambda self, *a: None
    with patch("urllib.request.urlopen") as urlopen_mock:
        urlopen_mock.return_value = mock_resp
        result = _send_validation_alert_best_effort("error")
    assert result == "skipped:http_error"
    ack_file = state_dir / "last_alerted_hash"
    assert not ack_file.exists(), "ACK must NOT be written on non-200"


def test_send_alert_skipped_on_connection_error(monkeypatch, tmp_path):
    from scout.config_alert import _send_validation_alert_best_effort
    state_dir, env_file = _set_paths(monkeypatch, tmp_path)
    _seed_env_file(env_file)
    with patch("urllib.request.urlopen") as urlopen_mock:
        urlopen_mock.side_effect = URLError("connection refused")
        result = _send_validation_alert_best_effort("error")
    assert result == "skipped:http_error"
    ack_file = state_dir / "last_alerted_hash"
    assert not ack_file.exists()


def test_send_alert_never_raises_on_unexpected_exception(monkeypatch, tmp_path):
    """Plan R2 C2 fold: scoped patch to scout.config_alert.hashlib.sha256, NOT global."""
    from scout.config_alert import _send_validation_alert_best_effort
    _, env_file = _set_paths(monkeypatch, tmp_path)
    _seed_env_file(env_file)
    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr("scout.config_alert.hashlib.sha256", _boom)
    # Must NOT raise; must return the skipped:exception code
    result = _send_validation_alert_best_effort("error")
    assert result == "skipped:exception"


def test_send_alert_returns_sent_when_ack_write_fails_post_200(monkeypatch, tmp_path):
    """Plan R2 I4 fold: "sent" semantics preserved even if ack-write fails
    post-HTTP-200 (operator was notified; dedup loss next cycle is acceptable).
    """
    from scout.config_alert import _send_validation_alert_best_effort
    state_dir, env_file = _set_paths(monkeypatch, tmp_path)
    _seed_env_file(env_file)
    # Force write_text to fail by making state_dir a file after mkdir succeeds
    state_dir.mkdir()
    # Replace ack-file with a directory so write_text raises IsADirectoryError
    (state_dir / "last_alerted_hash").mkdir()
    with patch("urllib.request.urlopen") as urlopen_mock:
        urlopen_mock.return_value = _mock_urlopen_200()
        result = _send_validation_alert_best_effort("error")
    assert result == "sent"
    urlopen_mock.assert_called_once()


# ---------------------------------------------------------------------------
# .env file parsing edge cases (R2 I2 fold)
# ---------------------------------------------------------------------------


def test_read_env_value_tolerates_leading_whitespace(monkeypatch, tmp_path):
    from scout.config_alert import _send_validation_alert_best_effort
    _, env_file = _set_paths(monkeypatch, tmp_path)
    env_file.write_text(
        "  TELEGRAM_BOT_TOKEN=indented_token\n\tTELEGRAM_CHAT_ID=tab_chat\n",
        encoding="utf-8",
    )
    with patch("urllib.request.urlopen") as urlopen_mock:
        urlopen_mock.return_value = _mock_urlopen_200()
        result = _send_validation_alert_best_effort("error")
    assert result == "sent"
    req_arg = urlopen_mock.call_args[0][0]
    assert "indented_token" in req_arg.full_url
    payload = json.loads(req_arg.data.decode("utf-8"))
    assert payload["chat_id"] == "tab_chat"


def test_read_env_value_handles_empty_file_and_empty_value_and_quoted_value(
    monkeypatch, tmp_path
):
    """Plan R2 I2 fold: edge cases for .env parser."""
    from scout.config_alert import _read_env_value

    # Empty file
    empty_file = tmp_path / "empty.env"
    empty_file.write_text("", encoding="utf-8")
    assert _read_env_value("TELEGRAM_BOT_TOKEN", empty_file) is None

    # Key with empty value
    empty_val_file = tmp_path / "empty_val.env"
    empty_val_file.write_text("TELEGRAM_BOT_TOKEN=\n", encoding="utf-8")
    assert _read_env_value("TELEGRAM_BOT_TOKEN", empty_val_file) == ""

    # Quoted value (double quotes)
    dq_file = tmp_path / "dq.env"
    dq_file.write_text('TELEGRAM_BOT_TOKEN="quoted-token"\n', encoding="utf-8")
    assert _read_env_value("TELEGRAM_BOT_TOKEN", dq_file) == "quoted-token"

    # Quoted value (single quotes)
    sq_file = tmp_path / "sq.env"
    sq_file.write_text("TELEGRAM_BOT_TOKEN='single-token'\n", encoding="utf-8")
    assert _read_env_value("TELEGRAM_BOT_TOKEN", sq_file) == "single-token"

    # Missing file
    missing = tmp_path / "does-not-exist.env"
    assert _read_env_value("TELEGRAM_BOT_TOKEN", missing) is None


# ---------------------------------------------------------------------------
# Body truncation
# ---------------------------------------------------------------------------


def test_send_alert_truncates_oversized_error_to_3800_chars(monkeypatch, tmp_path):
    """Plan R2 I3 fold: error_str > 3800 chars must be truncated to keep
    Telegram payload under the 4096-byte limit."""
    from scout.config_alert import _send_validation_alert_best_effort
    _, env_file = _set_paths(monkeypatch, tmp_path)
    _seed_env_file(env_file)
    huge_err = "x" * 5000
    with patch("urllib.request.urlopen") as urlopen_mock:
        urlopen_mock.return_value = _mock_urlopen_200()
        result = _send_validation_alert_best_effort(huge_err)
    assert result == "sent"
    req_arg = urlopen_mock.call_args[0][0]
    payload = json.loads(req_arg.data.decode("utf-8"))
    # Body = "⚠️ settings_validation_failed\n" + first 3800 chars of error_str
    assert len(payload["text"]) <= 3900  # header + 3800 + margin
    assert payload["text"].count("x") == 3800


# ---------------------------------------------------------------------------
# load_settings integration (Plan R2 C1 fold: patch SOURCE module attribute)
# ---------------------------------------------------------------------------


def test_load_settings_invokes_alert_helper_on_validation_error(monkeypatch, tmp_path):
    """Plan R2 C1 fold: the local import inside load_settings() resolves
    `scout.config_alert._send_validation_alert_best_effort` — so the correct
    mock target is the SOURCE module attribute (not scout.config.*, which
    has no module-level binding for this helper).
    """
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    from scout.config import load_settings
    captured: list[str] = []

    def _fake_helper(error_str: str) -> str:
        captured.append(error_str)
        return "sent"

    monkeypatch.setattr(
        "scout.config_alert._send_validation_alert_best_effort", _fake_helper
    )
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        # Trigger a known validator failure
        load_settings(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            HELD_POSITION_STALE_WARN_HOURS=99999,  # rejected by validator (>168)
        )
    assert len(captured) == 1
    assert "HELD_POSITION_STALE_WARN_HOURS" in captured[0]


def test_load_settings_logs_alert_outcome_on_validation_error(monkeypatch, tmp_path):
    """PR-#160 R2 MINOR-2 fold: outcome of _send_validation_alert_best_effort
    must be logged as a structured event (`settings_validation_alert_dispatched`)
    so silent-skip paths (e.g. "skipped:no_creds") are visible in journalctl,
    not lost.
    """
    import structlog
    from pydantic import ValidationError
    from scout.config import load_settings

    monkeypatch.setattr(
        "scout.config_alert._send_validation_alert_best_effort",
        lambda err: "skipped:no_creds",
    )
    with structlog.testing.capture_logs() as cap_logs:
        with pytest.raises(ValidationError):
            load_settings(
                _env_file=None,
                TELEGRAM_BOT_TOKEN="t",
                TELEGRAM_CHAT_ID="c",
                HELD_POSITION_STALE_WARN_HOURS=99999,
            )
    dispatched = [
        e for e in cap_logs
        if e.get("event") == "settings_validation_alert_dispatched"
    ]
    assert len(dispatched) == 1
    assert dispatched[0]["outcome"] == "skipped:no_creds"


def test_load_settings_does_NOT_invoke_alert_helper_on_success(monkeypatch, tmp_path):
    """Negative path: successful Settings() construction must NOT trigger alert."""
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    from scout.config import load_settings
    called = []

    def _fake_helper(error_str: str) -> str:
        called.append(error_str)
        return "sent"

    monkeypatch.setattr(
        "scout.config_alert._send_validation_alert_best_effort", _fake_helper
    )
    s = load_settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    assert s.TELEGRAM_BOT_TOKEN == "t"
    assert called == []
