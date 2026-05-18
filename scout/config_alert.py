"""Best-effort curl-direct Telegram alert for settings_validation_failed events.

Imported by scout/config.py load_settings(). Fires synchronous urllib.request
to Telegram on validation failure, before the re-raise. NEVER blocks the
re-raise — all exceptions caught and swallowed.

Dedup via state file: SHA256(error_str) hashed; if same hash as last alert,
skip (avoids 360 msg/hr storm under systemd Restart=always crash-loop).

Does NOT depend on Settings being loaded (Settings IS the thing that's
broken). Reads TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID directly from os.environ
OR (if absent there) parses .env file by hand. NOTE: under systemd-managed
startup (`gecko-pipeline.service`) the os.environ layer is dead-code (no
`EnvironmentFile=` directive); kept for test injection + manual invocation.

§12b compliance: plain text body, no parse_mode field in payload.

BL-NEW-SETTINGS-VALIDATION-ALERT (cycle 14).
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_STATE_DIR = Path("/var/lib/gecko-alpha/settings-validation-watchdog")
# PR-#160 R1 I1 fold: derive .env default from this module's location (walk up
# two levels: scout/config_alert.py → scout/ → project root) instead of
# hardcoding /root/gecko-alpha/.env. Hardcoded path silently breaks on any
# non-srilu-VPS deploy (dev box, second VPS, CI runner, /opt/* installs); the
# `.env` fallback is the ONLY working creds source under systemd (no
# EnvironmentFile= directive), so a wrong default == feature silently inert.
DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
ALERT_URL_FMT = "https://api.telegram.org/bot{token}/sendMessage"
# Plan R1 I1 fold: 3s ceiling. systemd Restart=always+RestartSec=10 means
# a longer timeout would double the crash-loop period (10s sleep + 10s
# timeout = ~20s) on every restart while Telegram is unreachable. 3s is
# best-effort-grade; failure path returns "skipped:http_error" silently.
ALERT_TIMEOUT_SEC = 3
MAX_ERROR_BODY_CHARS = 3800  # leave headroom under Telegram 4096-byte cap


def _read_env_value(key: str, env_file: Path) -> str | None:
    """Read KEY=value from .env, tolerating leading whitespace.
    Returns None if file missing or key not found.
    Strips surrounding quotes (one layer of single OR double).
    """
    if not env_file.exists():
        return None
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.lstrip()
            if stripped.startswith(f"{key}="):
                val = stripped[len(key) + 1:]
                val = val.rstrip()
                # Strip ONE layer of matching quotes
                if len(val) >= 2 and (
                    (val[0] == '"' and val[-1] == '"')
                    or (val[0] == "'" and val[-1] == "'")
                ):
                    val = val[1:-1]
                return val
    except OSError:
        return None
    return None


def _resolve_telegram_creds(env_file: Path) -> tuple[str | None, str | None]:
    """Read TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID from os.environ or .env.
    Returns (None, None) if either missing/placeholder.
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or _read_env_value(
        "TELEGRAM_BOT_TOKEN", env_file
    )
    chat = os.environ.get("TELEGRAM_CHAT_ID") or _read_env_value(
        "TELEGRAM_CHAT_ID", env_file
    )
    if not token or token == "placeholder":
        return None, None
    if not chat or chat == "placeholder":
        return None, None
    return token, chat


def _send_validation_alert_best_effort(error_str: str) -> str:
    """Send a plain-text Telegram alert on settings_validation_failed.

    Returns one of: "sent", "skipped:no_creds", "skipped:dedup",
    "skipped:state_dir_unwritable", "skipped:http_error",
    "skipped:exception". Never raises.
    """
    try:
        state_dir = Path(
            os.environ.get(
                "SETTINGS_VALIDATION_ALERT_STATE_DIR", str(DEFAULT_STATE_DIR)
            )
        )
        env_file = Path(
            os.environ.get("GECKO_ENV_FILE", str(DEFAULT_ENV_FILE))
        )

        token, chat = _resolve_telegram_creds(env_file)
        if token is None or chat is None:
            return "skipped:no_creds"

        # Compute dedup hash BEFORE attempting state-dir creation so that an
        # unwritable state-dir on the first call doesn't poison the hash
        # comparison on subsequent calls.
        error_hash = hashlib.sha256(error_str.encode("utf-8")).hexdigest()

        try:
            state_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return "skipped:state_dir_unwritable"
        ack_file = state_dir / "last_alerted_hash"
        if ack_file.exists() and ack_file.is_file():
            try:
                prior = ack_file.read_text(encoding="utf-8").strip()
                if prior == error_hash:
                    return "skipped:dedup"
            except OSError:
                pass  # unreadable ack treated as "no prior"

        # Plain-text body. NO parse_mode (per CLAUDE.md §12b — Pydantic
        # error strings contain underscores that MarkdownV1 would mangle).
        body_text = "⚠️ settings_validation_failed\n" + error_str[:MAX_ERROR_BODY_CHARS]
        payload = json.dumps({"chat_id": chat, "text": body_text}).encode("utf-8")
        req = urllib.request.Request(
            ALERT_URL_FMT.format(token=token),
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=ALERT_TIMEOUT_SEC) as resp:
                if resp.status != 200:
                    return "skipped:http_error"
        except (urllib.error.URLError, OSError, TimeoutError):
            return "skipped:http_error"

        # Write ack ONLY on successful HTTP 200. Failure to write ack here
        # does NOT degrade the "sent" semantic — operator was notified;
        # dedup loss next cycle is acceptable.
        try:
            ack_file.write_text(error_hash, encoding="utf-8")
        except OSError:
            pass
        return "sent"
    except Exception:
        return "skipped:exception"
