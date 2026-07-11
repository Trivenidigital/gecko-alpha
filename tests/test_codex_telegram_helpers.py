from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts.codex_systemd_failure_alert import build_failure_message, normalize_alert_unit_name
from scripts.codex_systemd_auto_remediate import (
    RemediationContext,
    RemediationPolicy,
    acquire_lock,
    release_lock,
    remediate_unit,
    with_allowlist,
)
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


def test_alert_unit_normalization_recovers_legacy_slash_name():
    assert normalize_alert_unit_name("hermes-gateway.service") == "hermes-gateway.service"
    assert normalize_alert_unit_name("hermes/gateway.service") == "hermes-gateway.service"


class FakeRunner:
    def __init__(self, show: dict[str, str], active_sequence: list[str] | None = None):
        self.show = show
        self.active_sequence = list(active_sequence or ["failed"])
        self.commands: list[tuple[str, ...]] = []

    def __call__(self, command: list[str], timeout: int = 15) -> str:
        self.commands.append(tuple(command))
        if command[:2] == ["systemctl", "show"]:
            return "\n".join(f"{k}={v}" for k, v in self.show.items())
        if command[:2] == ["systemctl", "is-active"]:
            if self.active_sequence:
                return self.active_sequence.pop(0)
            return "failed"
        if command[:2] == ["journalctl", "-u"]:
            return "journal line"
        return ""


class RaisingRunner(FakeRunner):
    def __init__(self, show: dict[str, str], raise_on: str):
        super().__init__(show)
        self.raise_on = raise_on

    def __call__(self, command: list[str], timeout: int = 15) -> str:
        self.commands.append(tuple(command))
        if self.raise_on in command:
            raise RuntimeError(f"{self.raise_on} exploded")
        return super().__call__(command, timeout)


def test_remediator_rejects_slash_unit_without_mutating_systemd(tmp_path):
    runner = FakeRunner(
        {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "Type": "simple",
        }
    )
    sent: list[str] = []

    result = remediate_unit(
        "hermes/gateway.service",
        RemediationContext(
            host="main-vps",
            state_dir=tmp_path,
            lock_dir=tmp_path / "locks",
            audit_path=tmp_path / "audit.log",
            runner=runner,
            sender=sent.append,
            now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
            sleep=lambda _: None,
            allow_lock_fallback=True,
        ),
    )

    assert result.action == "skipped_invalid_unit"
    assert not any("reset-failed" in cmd or "start" in cmd for command in runner.commands for cmd in command)
    assert sent


def test_remediator_skips_unallowlisted_or_oneshot_units(tmp_path):
    sent: list[str] = []
    runner = FakeRunner(
        {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "Type": "oneshot",
        }
    )

    result = remediate_unit(
        "hermes-gateway.service",
        RemediationContext(
            host="main-vps",
            state_dir=tmp_path,
            lock_dir=tmp_path / "locks",
            audit_path=tmp_path / "audit.log",
            runner=runner,
            sender=sent.append,
            now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
            sleep=lambda _: None,
            allow_lock_fallback=True,
        ),
    )

    assert result.action == "skipped_unsupported_type"
    assert not any("reset-failed" in command for command in runner.commands)


def test_remediator_skips_unallowlisted_unit_before_show(tmp_path):
    runner = FakeRunner(
        {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "Type": "simple",
        }
    )

    result = remediate_unit(
        "codex-production-push-loop-main.service",
        RemediationContext(
            host="main-vps",
            state_dir=tmp_path,
            lock_dir=tmp_path / "locks",
            audit_path=tmp_path / "audit.log",
            runner=runner,
            sender=lambda _: None,
            now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
            sleep=lambda _: None,
            allow_lock_fallback=True,
        ),
    )

    assert result.action == "skipped_unallowlisted"
    assert runner.commands == []


def test_remediator_skips_static_not_found_or_generated_states(tmp_path):
    for state in ["static", "generated", "transient", "bad", "not-found"]:
        runner = FakeRunner(
            {
                "LoadState": "loaded" if state != "not-found" else "not-found",
                "UnitFileState": state,
                "Type": "simple",
            }
        )
        result = remediate_unit(
            "hermes-gateway.service",
            RemediationContext(
                host="main-vps",
                state_dir=tmp_path / state,
                lock_dir=tmp_path / state / "locks",
                audit_path=tmp_path / state / "audit.log",
                runner=runner,
                sender=lambda _: None,
                now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
                sleep=lambda _: None,
                allow_lock_fallback=True,
            ),
        )

        assert result.action in {"skipped_bad_load_state", "skipped_unit_file_state"}
        assert not any("reset-failed" in command or "start" in command for command in runner.commands)


def test_remediator_skips_disabled_and_masked_units(tmp_path):
    for state in ["disabled", "masked"]:
        runner = FakeRunner(
            {
                "LoadState": "loaded",
                "UnitFileState": state,
                "Type": "simple",
            }
        )

        result = remediate_unit(
            "hermes-gateway.service",
            RemediationContext(
                host="main-vps",
                state_dir=tmp_path / state,
                lock_dir=tmp_path / state / "locks",
                audit_path=tmp_path / state / "audit.log",
                runner=runner,
                sender=lambda _: None,
                now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
                sleep=lambda _: None,
                allow_lock_fallback=True,
            ),
        )

        assert result.action == "skipped_unit_file_state"
        assert not any("reset-failed" in command or "start" in command for command in runner.commands)


def test_remediator_skips_handler_units_without_mutating(tmp_path):
    runner = FakeRunner(
        {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "Type": "simple",
        }
    )

    result = remediate_unit(
        "codex-systemd-auto-remediate@hermes-gateway.service.service",
        RemediationContext(
            host="main-vps",
            state_dir=tmp_path,
            lock_dir=tmp_path / "locks",
            audit_path=tmp_path / "audit.log",
            runner=runner,
            sender=lambda _: None,
            now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
            sleep=lambda _: None,
            allow_lock_fallback=True,
        ),
    )

    assert result.action == "skipped_handler_unit"
    assert runner.commands == []


def test_remediator_persists_cooldown_before_reset_and_start(tmp_path):
    runner = FakeRunner(
        {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "Type": "simple",
        },
        active_sequence=["failed", "active"],
    )
    sent: list[str] = []

    result = remediate_unit(
        "hermes-gateway.service",
        RemediationContext(
            host="main-vps",
            state_dir=tmp_path,
            lock_dir=tmp_path / "locks",
            audit_path=tmp_path / "audit.log",
            runner=runner,
            sender=sent.append,
            now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
            sleep=lambda _: None,
            allow_lock_fallback=True,
        ),
    )

    assert result.action == "repaired"
    cooldown_path = tmp_path / "hermes-gateway.service.last_attempt"
    assert cooldown_path.exists()
    assert tuple(["systemctl", "reset-failed", "hermes-gateway.service"]) in runner.commands
    assert tuple(["systemctl", "start", "hermes-gateway.service"]) in runner.commands
    assert "parse_mode" not in "\n".join(sent)
    assert runner.commands.index(tuple(["systemctl", "reset-failed", "hermes-gateway.service"])) < runner.commands.index(
        tuple(["systemctl", "start", "hermes-gateway.service"])
    )
    assert cooldown_path.stat().st_size > 0


def test_remediator_cooldown_skips_without_mutating(tmp_path):
    now = datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc)
    (tmp_path / "hermes-gateway.service.last_attempt").write_text(now.isoformat(), encoding="utf-8")
    runner = FakeRunner(
        {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "Type": "simple",
        }
    )

    result = remediate_unit(
        "hermes-gateway.service",
        RemediationContext(
            host="main-vps",
            state_dir=tmp_path,
            lock_dir=tmp_path / "locks",
            audit_path=tmp_path / "audit.log",
            runner=runner,
            sender=lambda _: None,
            now=lambda: now,
            sleep=lambda _: None,
            allow_lock_fallback=True,
        ),
    )

    assert result.action == "skipped_cooldown"
    assert not any("reset-failed" in command or "start" in command for command in runner.commands)


def test_remediator_contended_lock_skips_without_mutating(tmp_path):
    if os.name == "nt":
        pytest.skip("fcntl advisory lock behavior is Linux-only")
    first_fd = acquire_lock("hermes-gateway.service", tmp_path / "locks")
    runner = FakeRunner(
        {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "Type": "simple",
        }
    )
    try:
        result = remediate_unit(
            "hermes-gateway.service",
            RemediationContext(
                host="main-vps",
                state_dir=tmp_path,
                lock_dir=tmp_path / "locks",
                audit_path=tmp_path / "audit.log",
                runner=runner,
                sender=lambda _: None,
                now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
                sleep=lambda _: None,
                allow_lock_fallback=True,
            ),
        )
    finally:
        release_lock(first_fd, "hermes-gateway.service", tmp_path / "locks")

    assert result.action == "skipped_locked"
    assert not any("reset-failed" in command or "start" in command for command in runner.commands)


def test_remediator_failed_cooldown_state_fails_closed(monkeypatch, tmp_path):
    runner = FakeRunner(
        {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "Type": "simple",
        }
    )

    def broken_mkdir(*_: object, **__: object) -> None:
        raise OSError("state unavailable")

    monkeypatch.setattr(Path, "mkdir", broken_mkdir)

    result = remediate_unit(
        "hermes-gateway.service",
        RemediationContext(
            host="main-vps",
            state_dir=tmp_path,
            lock_dir=tmp_path / "locks",
            audit_path=tmp_path / "audit.log",
            runner=runner,
            sender=lambda _: None,
            now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
            sleep=lambda _: None,
            allow_lock_fallback=True,
        ),
    )

    assert result.action == "skipped_state_unavailable"
    assert not any("reset-failed" in command or "start" in command for command in runner.commands)


def test_remediator_telegram_failure_does_not_block_repair(tmp_path):
    runner = FakeRunner(
        {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "Type": "simple",
        },
        active_sequence=["active"],
    )

    def broken_sender(_: str) -> None:
        raise RuntimeError("telegram down")

    result = remediate_unit(
        "hermes-gateway.service",
        RemediationContext(
            host="main-vps",
            state_dir=tmp_path,
            lock_dir=tmp_path / "locks",
            audit_path=tmp_path / "audit.log",
            runner=runner,
            sender=broken_sender,
            now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
            sleep=lambda _: None,
            allow_lock_fallback=True,
        ),
    )

    assert result.action == "repaired"
    assert tuple(["systemctl", "reset-failed", "hermes-gateway.service"]) in runner.commands
    assert "telegram down" in (tmp_path / "audit.log").read_text(encoding="utf-8")
    row = json.loads((tmp_path / "audit.log").read_text(encoding="utf-8").splitlines()[-1])
    assert row["unit"] == "hermes-gateway.service"
    assert row["action"] == "repaired"
    assert row["status"] == "active"
    assert row["telegram_errors"] == ["telegram down"]


def test_remediator_still_failed_needs_operator_action(tmp_path):
    runner = FakeRunner(
        {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "Type": "simple",
        },
        active_sequence=["failed", "failed", "failed"],
    )

    result = remediate_unit(
        "hermes-gateway.service",
        RemediationContext(
            host="main-vps",
            state_dir=tmp_path,
            lock_dir=tmp_path / "locks",
            audit_path=tmp_path / "audit.log",
            runner=runner,
            sender=lambda _: None,
            now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
            sleep=lambda _: None,
            allow_lock_fallback=True,
        ),
        policy=RemediationPolicy(poll_attempts=2, poll_seconds=1),
    )

    assert result.action == "needs_operator_action"


def test_remediator_extra_allowlist_supports_disposable_verification(tmp_path):
    runner = FakeRunner(
        {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "Type": "simple",
        },
        active_sequence=["active"],
    )

    context = with_allowlist(
        RemediationContext(
            host="main-vps",
            state_dir=tmp_path,
            lock_dir=tmp_path / "locks",
            audit_path=tmp_path / "audit.log",
            runner=runner,
            sender=lambda _: None,
            now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
            sleep=lambda _: None,
            allow_lock_fallback=True,
        ),
        {"codex-remediation-flaky.service"},
    )

    result = remediate_unit("codex-remediation-flaky.service", context)

    assert result.action == "repaired"
    assert tuple(["systemctl", "start", "codex-remediation-flaky.service"]) in runner.commands


def test_remediator_runner_exception_before_mutation_is_audited_skip(tmp_path):
    runner = RaisingRunner(
        {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "Type": "simple",
        },
        "show",
    )

    result = remediate_unit(
        "hermes-gateway.service",
        RemediationContext(
            host="main-vps",
            state_dir=tmp_path,
            lock_dir=tmp_path / "locks",
            audit_path=tmp_path / "audit.log",
            runner=runner,
            sender=lambda _: None,
            now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
            sleep=lambda _: None,
            allow_lock_fallback=True,
        ),
    )

    assert result.action == "skipped_state_unavailable"
    assert "systemctl show failed" in result.reason
    assert "reset-failed" not in " ".join(" ".join(command) for command in runner.commands)
    assert "skipped_state_unavailable" in (tmp_path / "audit.log").read_text(encoding="utf-8")


def test_remediator_runner_exception_after_mutation_needs_operator_action(tmp_path):
    runner = RaisingRunner(
        {
            "LoadState": "loaded",
            "UnitFileState": "enabled",
            "Type": "simple",
        },
        "start",
    )

    result = remediate_unit(
        "hermes-gateway.service",
        RemediationContext(
            host="main-vps",
            state_dir=tmp_path,
            lock_dir=tmp_path / "locks",
            audit_path=tmp_path / "audit.log",
            runner=runner,
            sender=lambda _: None,
            now=lambda: datetime(2026, 5, 23, 15, 54, tzinfo=timezone.utc),
            sleep=lambda _: None,
            allow_lock_fallback=True,
        ),
    )

    assert result.action == "needs_operator_action"
    assert tuple(["systemctl", "reset-failed", "hermes-gateway.service"]) in runner.commands
    assert "start exploded" in (tmp_path / "audit.log").read_text(encoding="utf-8")


def test_advisory_lock_allows_preexisting_unlocked_file(tmp_path):
    lock_dir = tmp_path / "locks"
    lock_dir.mkdir()
    (lock_dir / "hermes-gateway.service.lock").write_text("stale", encoding="utf-8")

    fd = acquire_lock("hermes-gateway.service", lock_dir)
    try:
        assert isinstance(fd, int)
    finally:
        release_lock(fd, "hermes-gateway.service", lock_dir)


