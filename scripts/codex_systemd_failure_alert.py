#!/usr/bin/env python3
"""Send a Telegram alert for a failed Codex/Hermes systemd unit."""

from __future__ import annotations

import argparse
import socket
import subprocess
from datetime import datetime, timezone


def fmt_time(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def build_failure_message(
    unit: str,
    host: str,
    now: datetime,
    status: str,
    journal_tail: str,
) -> str:
    tail = journal_tail.strip()
    if len(tail) > 2500:
        tail = tail[-2500:]
    return (
        "Codex/Hermes unit failure\n\n"
        f"time: {fmt_time(now)}\n"
        f"host: {host}\n"
        f"unit: {unit}\n"
        f"status: {status}\n\n"
        "Recent journal:\n"
        f"{tail or '[no journal output]'}"
    )


def normalize_alert_unit_name(unit: str) -> str:
    if "/" in unit and unit.endswith(".service"):
        return unit.replace("/", "-")
    return unit


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("unit")
    parser.add_argument("--host", default=socket.gethostname())
    args = parser.parse_args(argv)
    unit = normalize_alert_unit_name(args.unit)

    status = run_text(["systemctl", "is-active", unit]).strip() or "unknown"
    journal = run_text(["journalctl", "-u", unit, "-n", "30", "--no-pager"])
    message = build_failure_message(
        unit=unit,
        host=args.host,
        now=datetime.now(timezone.utc),
        status=status,
        journal_tail=journal,
    )
    # 30s timeout bounds the OnFailure-alert chain. codex-telegram-send
    # has its own 20s urlopen timeout, but a caller-side bound protects
    # against pathological Python startup / binary-replacement scenarios
    # where the inner timeout doesn't fire fast enough. Without this
    # bound, systemd OnFailure handlers could hang indefinitely.
    subprocess.run(
        ["/usr/local/bin/codex-telegram-send"],
        input=message,
        text=True,
        check=True,
        timeout=30,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
