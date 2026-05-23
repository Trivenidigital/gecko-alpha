#!/usr/bin/env python3
"""Guarded auto-remediation for failed Codex/Hermes systemd units."""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable


REPAIR_ALLOWLIST = {
    "hermes-gateway.service",
    "gecko-pipeline.service",
    "gecko-dashboard.service",
    "nginx.service",
    "shift-agent-cockpit.service",
}
ALLOWED_UNIT_FILE_STATES = {"enabled", "enabled-runtime"}
ALLOWED_SERVICE_TYPES = {"simple", "notify"}
HANDLER_PREFIXES = (
    "codex-systemd-failure-alert@",
    "codex-systemd-auto-remediate@",
)


@dataclass(frozen=True)
class RemediationPolicy:
    cooldown_minutes: int = 30
    poll_attempts: int = 6
    poll_seconds: int = 10
    repair_allowlist: frozenset[str] = frozenset(REPAIR_ALLOWLIST)


@dataclass
class RemediationResult:
    unit: str
    action: str
    reason: str
    status: str = "unknown"
    telegram_errors: list[str] | None = None


@dataclass
class RemediationContext:
    host: str
    state_dir: Path
    audit_path: Path
    runner: Callable[[list[str], int], str]
    sender: Callable[[str], None]
    now: Callable[[], datetime]
    sleep: Callable[[float], None]


def fmt_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def run_text(command: list[str], timeout: int = 15) -> str:
    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    return result.stdout


def send_telegram(message: str) -> None:
    subprocess.run(
        ["/usr/local/bin/codex-telegram-send"],
        input=message,
        text=True,
        check=True,
    )


def default_context(host: str) -> RemediationContext:
    return RemediationContext(
        host=host,
        state_dir=Path("/var/lib/codex-remediation"),
        audit_path=Path("/var/log/codex-remediation.log"),
        runner=run_text,
        sender=send_telegram,
        now=lambda: datetime.now(timezone.utc),
        sleep=__import__("time").sleep,
    )


def parse_show_output(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def is_handler_unit(unit: str) -> bool:
    return any(unit.startswith(prefix) for prefix in HANDLER_PREFIXES)


def state_file_for(state_dir: Path, unit: str) -> Path:
    return state_dir / f"{unit}.last_attempt"


def acquire_lock(unit: str) -> int | None:
    lock_dir = Path("/run/codex-remediation")
    try:
        lock_dir.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_dir / f"{unit}.lock", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        os.write(fd, str(os.getpid()).encode("ascii"))
        return fd
    except FileExistsError:
        return None
    except OSError:
        return -1


def release_lock(fd: int | None, unit: str) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    finally:
        try:
            (Path("/run/codex-remediation") / f"{unit}.lock").unlink()
        except FileNotFoundError:
            pass


def cooldown_remaining(
    unit: str,
    context: RemediationContext,
    policy: RemediationPolicy,
) -> timedelta | None:
    path = state_file_for(context.state_dir, unit)
    if not path.exists():
        return None
    attempted_at = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    elapsed = context.now().astimezone(timezone.utc) - attempted_at.astimezone(timezone.utc)
    remaining = timedelta(minutes=policy.cooldown_minutes) - elapsed
    return remaining if remaining.total_seconds() > 0 else None


def persist_attempt_timestamp(unit: str, context: RemediationContext) -> None:
    context.state_dir.mkdir(parents=True, exist_ok=True)
    path = state_file_for(context.state_dir, unit)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(context.now().astimezone(timezone.utc).isoformat(), encoding="utf-8")
    temp_path.replace(path)


def build_message(result: RemediationResult, context: RemediationContext) -> str:
    return (
        "Codex/Hermes auto-remediation\n\n"
        f"time: {fmt_time(context.now())}\n"
        f"host: {context.host}\n"
        f"unit: {result.unit}\n"
        f"action: {result.action}\n"
        f"status: {result.status}\n"
        f"reason: {result.reason}"
    )


def send_best_effort(
    result: RemediationResult,
    context: RemediationContext,
    telegram_errors: list[str],
) -> None:
    try:
        context.sender(build_message(result, context))
    except Exception as exc:  # pragma: no cover - exact sender failures vary
        telegram_errors.append(str(exc))


def append_audit(result: RemediationResult, context: RemediationContext) -> None:
    row = {
        "time": fmt_time(context.now()),
        "host": context.host,
        "unit": result.unit,
        "action": result.action,
        "reason": result.reason,
        "status": result.status,
        "telegram_errors": result.telegram_errors or [],
    }
    context.audit_path.parent.mkdir(parents=True, exist_ok=True)
    with context.audit_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")


def finish(
    result: RemediationResult,
    context: RemediationContext,
    telegram_errors: list[str],
) -> RemediationResult:
    send_best_effort(result, context, telegram_errors)
    result.telegram_errors = list(telegram_errors)
    try:
        append_audit(result, context)
    except Exception as exc:  # pragma: no cover - stderr fallback is environment dependent
        print(f"codex-systemd-auto-remediate: audit write failed: {exc}", file=sys.stderr)
    return result


def skip(unit: str, action: str, reason: str, context: RemediationContext, telegram_errors: list[str]) -> RemediationResult:
    return finish(RemediationResult(unit=unit, action=action, reason=reason), context, telegram_errors)


def context_allowlist(context: RemediationContext) -> set[str]:
    return getattr(context, "_repair_allowlist", set(REPAIR_ALLOWLIST))


def with_allowlist(context: RemediationContext, allowlist: set[str]) -> RemediationContext:
    setattr(context, "_repair_allowlist", allowlist)
    return context


def validate_unit(
    unit: str,
    context: RemediationContext,
    telegram_errors: list[str],
) -> tuple[bool, RemediationResult | None, dict[str, str]]:
    if "/" in unit:
        return False, skip(unit, "skipped_invalid_unit", "slash-containing unit names are rejected", context, telegram_errors), {}
    if not unit.endswith(".service"):
        return False, skip(unit, "skipped_invalid_unit", "only .service units are repairable", context, telegram_errors), {}
    if is_handler_unit(unit):
        return False, skip(unit, "skipped_handler_unit", "handler units are never remediated", context, telegram_errors), {}
    if unit not in context_allowlist(context):
        return False, skip(unit, "skipped_unallowlisted", "unit is not in repair allowlist", context, telegram_errors), {}

    show = parse_show_output(
        context.runner(
            [
                "systemctl",
                "show",
                unit,
                "-p",
                "LoadState",
                "-p",
                "UnitFileState",
                "-p",
                "Type",
                "--no-pager",
            ],
            15,
        )
    )
    if show.get("LoadState") != "loaded":
        return False, skip(unit, "skipped_bad_load_state", f"LoadState={show.get('LoadState', 'unknown')}", context, telegram_errors), show
    if show.get("UnitFileState") not in ALLOWED_UNIT_FILE_STATES:
        return False, skip(unit, "skipped_unit_file_state", f"UnitFileState={show.get('UnitFileState', 'unknown')}", context, telegram_errors), show
    if show.get("Type") not in ALLOWED_SERVICE_TYPES:
        return False, skip(unit, "skipped_unsupported_type", f"Type={show.get('Type', 'unknown')}", context, telegram_errors), show
    return True, None, show


def remediate_unit(
    unit: str,
    context: RemediationContext,
    policy: RemediationPolicy | None = None,
) -> RemediationResult:
    policy = policy or RemediationPolicy()
    telegram_errors: list[str] = []
    ok, early_result, _ = validate_unit(unit, context, telegram_errors)
    if not ok:
        return early_result  # type: ignore[return-value]

    lock_fd = acquire_lock(unit)
    if lock_fd == -1:
        return skip(unit, "skipped_state_unavailable", "lock state unavailable", context, telegram_errors)
    if lock_fd is None:
        return skip(unit, "skipped_locked", "another remediation is already running", context, telegram_errors)

    try:
        try:
            remaining = cooldown_remaining(unit, context, policy)
        except Exception as exc:
            return skip(unit, "skipped_state_unavailable", f"cooldown read failed: {exc}", context, telegram_errors)
        if remaining is not None:
            return skip(unit, "skipped_cooldown", f"cooldown remaining {int(remaining.total_seconds())}s", context, telegram_errors)

        try:
            persist_attempt_timestamp(unit, context)
        except Exception as exc:
            return skip(unit, "skipped_state_unavailable", f"cooldown write failed: {exc}", context, telegram_errors)

        started = RemediationResult(unit=unit, action="repair_started", reason="attempting reset-failed and start")
        send_best_effort(started, context, telegram_errors)
        context.runner(["systemctl", "reset-failed", unit], 20)
        context.runner(["systemctl", "start", unit], 60)
        status = "unknown"
        for _ in range(policy.poll_attempts):
            status = context.runner(["systemctl", "is-active", unit], 15).strip() or "unknown"
            if status == "active":
                return finish(
                    RemediationResult(unit=unit, action="repaired", reason="unit active after restart", status=status),
                    context,
                    telegram_errors,
                )
            context.sleep(policy.poll_seconds)

        journal = context.runner(["journalctl", "-u", unit, "-n", "20", "--no-pager"], 15).strip()
        reason = "unit did not become active"
        if journal:
            reason += "; recent journal: " + journal[-1000:]
        return finish(
            RemediationResult(unit=unit, action="needs_operator_action", reason=reason, status=status),
            context,
            telegram_errors,
        )
    finally:
        release_lock(lock_fd, unit)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unit")
    parser.add_argument("--host", default=socket.gethostname())
    parser.add_argument(
        "--allow-unit",
        action="append",
        default=[],
        help="extra repair-allowed unit for disposable verification only",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    allowlist = set(REPAIR_ALLOWLIST)
    allowlist.update(args.allow_unit)
    result = remediate_unit(args.unit, with_allowlist(default_context(args.host), allowlist))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
