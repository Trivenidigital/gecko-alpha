#!/usr/bin/env bash
set -euo pipefail

DB_PATH="scout.db"
THRESHOLD_MINUTES="30"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db)
      DB_PATH="${2:?--db requires a path}"
      shift 2
      ;;
    --threshold-minutes)
      THRESHOLD_MINUTES="${2:?--threshold-minutes requires a value}"
      shift 2
      ;;
    *)
      echo "unknown argument: $1" >&2
      exit 64
      ;;
  esac
done

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

exec python "${SCRIPT_DIR}/check_source_calls_lag.py" \
  --db "${DB_PATH}" \
  --threshold-minutes "${THRESHOLD_MINUTES}"
