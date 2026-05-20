#!/usr/bin/env bash
# Periodic source_calls ledger writer.
# Calls backfill_source_calls + refresh_source_call_outcomes via
# scripts/source_calls_live_writer.py. Idempotent.
#
# No Telegram dispatch — operator-visible alerting lives in
# scripts/source-calls-lag-watchdog.sh (single alerter surface, §12a).
#
# Exit codes:
#   0  — success
#   1  — DB missing or runtime error (see stdout JSON for detail)
#  64  — unknown argument

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
DB_PATH="${REPO_ROOT}/scout.db"
PYTHON="${GECKO_PYTHON:-${REPO_ROOT}/.venv/bin/python}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db)
      DB_PATH="${2:?--db requires a path}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 64
      ;;
  esac
done

cd "$REPO_ROOT"
exec "${PYTHON}" "${SCRIPT_DIR}/source_calls_live_writer.py" --db "${DB_PATH}"
