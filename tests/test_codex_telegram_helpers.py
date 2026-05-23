from __future__ import annotations

from datetime import datetime, timezone

from scripts.codex_systemd_failure_alert import build_failure_message
from scripts.codex_telegram_send import build_payload, load_env


def test_telegram_payload_omits_parse_mode():
    payload = build_payload("12345", "hello_gain_early")

    assert payload == {"chat_id": "12345", "text": "hello_gain_early"}
    assert "parse_mode" not in payload


def test_load_env_reads_telegram_values_without_exposing_other_lines(tmp_path):
    env_file = tmp_path / "telegram.env"
    env_file.write_text(
        "TELEGRAM_BOT_TOKEN=secret-token\n"
        "TELEGRAM_CHAT_ID=12345\n"
        "OTHER=value\n",
        encoding="utf-8",
    )

    assert load_env(env_file) == {
        "TELEGRAM_BOT_TOKEN": "secret-token",
        "TELEGRAM_CHAT_ID": "12345",
        "OTHER": "value",
    }


def test_failure_message_is_plain_text_and_includes_unit_host_and_tail():
    now = datetime(2026, 5, 23, 14, 41, tzinfo=timezone.utc)

    message = build_failure_message(
        unit="codex-demo.service",
        host="main-vps",
        now=now,
        status="failed",
        journal_tail="line one\nline_two_with_underscore",
    )

    assert "Codex/Hermes unit failure" in message
    assert "host: main-vps" in message
    assert "unit: codex-demo.service" in message
    assert "status: failed" in message
    assert "2026-05-23T14:41:00Z" in message
    assert "line_two_with_underscore" in message
    assert "parse_mode" not in message
