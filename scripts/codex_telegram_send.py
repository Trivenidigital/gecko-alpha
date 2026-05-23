#!/usr/bin/env python3
"""Plain-text Telegram sender for Codex/Hermes operator alerts."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib import request


DEFAULT_ENV = Path("/etc/codex-telegram.env")


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"").strip("'")
    return values


def build_payload(chat_id: str, text: str) -> dict[str, str]:
    return {"chat_id": chat_id, "text": text}


def send_message(message: str, env_file: Path = DEFAULT_ENV) -> None:
    env = load_env(env_file)
    token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = env.get("TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError(f"Telegram credentials missing in {env_file}")

    data = json.dumps(build_payload(chat_id, message)).encode("utf-8")
    req = request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with request.urlopen(req, timeout=20) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"Telegram send failed HTTP {resp.status}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("message", nargs="*")
    args = parser.parse_args(argv)

    message = " ".join(args.message).strip()
    if not message:
        message = sys.stdin.read().strip()
    if not message:
        raise SystemExit("message is empty")
    send_message(message, args.env_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
