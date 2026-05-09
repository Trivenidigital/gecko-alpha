#!/usr/bin/env bash
# gecko-backup-watchdog — alert if rotation hasn't run successfully in 48h.
#
# Use absolute /root/.local/bin/uv (not bare `uv`) because systemd Type=oneshot
# units have a stripped PATH — verified against gecko-pipeline.service which
# also uses absolute uv. UV_BIN is also the testability seam: the pytest
# harness overrides it to a stub bash script for end-to-end watchdog tests.

set -euo pipefail

HEARTBEAT_FILE="${GECKO_BACKUP_HEARTBEAT_FILE:-/var/lib/gecko-alpha/backup-last-ok}"
STALE_AFTER_SEC="${GECKO_BACKUP_STALE_AFTER_SEC:-172800}"  # 48h
GECKO_REPO="${GECKO_REPO:-/root/gecko-alpha}"
UV_BIN="${UV_BIN:-/root/.local/bin/uv}"

now=$(date +%s)

if [[ ! -f "$HEARTBEAT_FILE" ]]; then
    age_msg="heartbeat file MISSING ($HEARTBEAT_FILE)"
    is_stale=1
else
    last_ok=$(cat "$HEARTBEAT_FILE")
    age_sec=$(( now - last_ok ))
    age_msg="last_ok=${age_sec}s ago"
    if (( age_sec > STALE_AFTER_SEC )); then
        is_stale=1
    else
        is_stale=0
    fi
fi

if (( is_stale == 1 )); then
    echo "STALE: gecko-backup-rotate has not run successfully — $age_msg"
    cd "$GECKO_REPO"
    # Use the existing project Telegram alerter so credentials + chat_id come
    # from .env and don't need duplicating into a sidecar.
    "$UV_BIN" run python -c "
import asyncio
from scout.alerter import send_telegram_message
from scout.config import Settings
async def go():
    s = Settings()
    await send_telegram_message(
        s,
        f'⚠️ gecko-backup-watchdog: rotation stale — $age_msg. '
        f'Check journalctl -u gecko-backup.service.'
    )
asyncio.run(go())
"
    exit 1
fi

echo "OK: gecko-backup-rotate ran within ${STALE_AFTER_SEC}s ($age_msg)"
