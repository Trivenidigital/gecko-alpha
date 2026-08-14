#!/usr/bin/env bash
# Forward-only CA price-snapshot writer cron entry (design #392 C2).
# Calls scripts/source_call_price_snapshots_writer.py to fetch current
# GeckoTerminal prices by contract address for active eligible_contract X
# source_calls and append source-tagged rows to source_call_price_snapshots.
#
# DEPLOY-WITHOUT-ACTIVATE: gated on SOURCE_CALL_SNAPSHOT_WRITER_ENABLED (from
# .env, default false). Until the operator flips it true the writer exits 0 as
# a no-op. No deploy/activation during the DEX soak without separate approval.
#
# No Telegram dispatch here — a freshness/coverage watchdog (C4) owns alerting
# (single alerter surface, §12a).
#
# Optional heartbeat: set SCPS_WRITER_HEARTBEAT_FILE (or --heartbeat-file PATH)
# for §12a cron-tick detection by the (C4) freshness watchdog.
#
# Exit codes:
#   0  — success (including the disabled no-op)
#   1  — DB missing or runtime error (see stdout JSON for detail)
#  64  — unknown argument

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON="${GECKO_PYTHON:-${REPO_ROOT}/.venv/bin/python}"

# Source .env FIRST so cron (sparse env) sees the enable flag + heartbeat path.
# DB_PATH is set with an absolute fallback AFTER sourcing so any relative
# DB_PATH in .env cannot clobber it (same guard as source-calls-live-writer.sh).
ENV_FILE="${GECKO_ENV_FILE:-${REPO_ROOT}/.env}"
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
fi

DB_PATH="${REPO_ROOT}/scout.db"
ENABLED="${SOURCE_CALL_SNAPSHOT_WRITER_ENABLED:-false}"
HORIZON_HOURS="${SOURCE_CALL_SNAPSHOT_HORIZON_HOURS:-28}"
MAX_IDENTITIES_PER_RUN="${SOURCE_CALL_SNAPSHOT_MAX_IDENTITIES_PER_RUN:-25}"
MAX_REQUESTS_PER_DAY="${SOURCE_CALL_SNAPSHOT_MAX_REQUESTS_PER_DAY:-2000}"
MAX_CG_IDENTITIES_PER_RUN="${SOURCE_CALL_SNAPSHOT_MAX_CG_IDENTITIES_PER_RUN:-32}"
MAX_PRICE_CACHE_AGE_MIN="${SOURCE_CALL_SNAPSHOT_MAX_PRICE_CACHE_AGE_MIN:-90}"
HEARTBEAT_FILE="${SCPS_WRITER_HEARTBEAT_FILE:-}"

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

# flock: two overlapping cycles allocate the SAME batch id (allocation reads
# MAX(batch_id) before either publishes), and the loser's marker would then
# either collide or publish its rows under the earlier cycle's visible_at —
# backdated visibility, the exact future leakage the batch mechanism exists to
# prevent. A slow cycle must be skipped, never run concurrently. House
# precedent: gecko-backup-create.sh, cron-drift-watchdog.sh.
LOCK_FILE="${SCPS_WRITER_LOCK_FILE:-/tmp/scps-writer.lock}"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "OK: skipping run — previous invocation still holds $LOCK_FILE"
    exit 0
fi

py_args=(
  --db "${DB_PATH}"
  --enabled "${ENABLED}"
  --horizon-hours "${HORIZON_HOURS}"
  --max-identities-per-run "${MAX_IDENTITIES_PER_RUN}"
  --max-requests-per-day "${MAX_REQUESTS_PER_DAY}"
  --max-cg-identities-per-run "${MAX_CG_IDENTITIES_PER_RUN}"
  --max-price-cache-age-min "${MAX_PRICE_CACHE_AGE_MIN}"
)
if [[ -n "$HEARTBEAT_FILE" ]]; then
    py_args+=(--heartbeat-file "$HEARTBEAT_FILE")
fi

exec "${PYTHON}" "${SCRIPT_DIR}/source_call_price_snapshots_writer.py" "${py_args[@]}"
