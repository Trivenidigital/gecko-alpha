#!/usr/bin/env bash
# Periodic source_calls ledger writer.
# Calls backfill_source_calls + refresh_source_call_outcomes via
# scripts/source_calls_live_writer.py. Idempotent.
#
# No Telegram dispatch — operator-visible alerting lives in
# scripts/source-calls-lag-watchdog.sh (single alerter surface, §12a).
#
# Optional heartbeat: set WRITER_HEARTBEAT_FILE in env (or pass
# --heartbeat-file PATH) to enable writer-cron-tick detection by the
# lag watchdog. Default empty -> no-op (back-compat).
#
# Exit codes:
#   0  — success
#   1  — DB missing or runtime error (see stdout JSON for detail)
#  64  — unknown argument

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${GECKO_PYTHON:-${REPO_ROOT}/.venv/bin/python}"

# Source .env FIRST so cron invocations see WRITER_HEARTBEAT_FILE.
# Cron's default env is sparse — without this source the wrapper would
# evaluate WRITER_HEARTBEAT_FILE as empty and skip the heartbeat touch,
# defeating the cron-tick watchdog. Discovered 2026-05-21 post-deploy.
#
# .env may contain a `DB_PATH=scout.db` (relative) line that, if
# auto-exported via `set -a`, would clobber DB_PATH after the wrapper
# sets it. So .env is sourced BEFORE local defaults, then DB_PATH is
# set with an absolute fallback that wins over any .env value.
ENV_FILE="${GECKO_ENV_FILE:-${REPO_ROOT}/.env}"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

DB_PATH="${REPO_ROOT}/scout.db"
HEARTBEAT_FILE="${WRITER_HEARTBEAT_FILE:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db)
      DB_PATH="${2:?--db requires a path}"
      shift 2
      ;;
    --heartbeat-file)
      HEARTBEAT_FILE="${2:?--heartbeat-file requires a path}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 64
      ;;
  esac
done

cd "$REPO_ROOT"

py_args=(--db "${DB_PATH}")
if [[ -n "$HEARTBEAT_FILE" ]]; then
    py_args+=(--heartbeat-file "$HEARTBEAT_FILE")
fi

exec "${PYTHON}" "${SCRIPT_DIR}/source_calls_live_writer.py" "${py_args[@]}"
