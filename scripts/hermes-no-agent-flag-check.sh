#!/usr/bin/env bash
# hermes-no-agent-flag-check — validate that the gecko-x-narrative-scanner
# Hermes cron job has not regressed its `no_agent: true` flag.
#
# BL-NEW-HERMES-CRON-NO-AGENT-FLAG-WATCHDOG (2026-05-20).
# Filed by PR #201; this script implements it as the cheapest possible
# guardrail per the operator's preference for "docs + operator command".
#
# Why: the May 15 prompt-injection failure mode was resolved by switching
# the cron from `agent` mode to `no_agent: true` shell-script mode. If a
# future PR or operator action flips it back to agent mode, the
# prompt-injection scanner will block the cron again. This script is a
# single-shot check that proves the flag is still correct.
#
# Usage:
#   scripts/hermes-no-agent-flag-check.sh
#   scripts/hermes-no-agent-flag-check.sh --quiet  # only emit on failure
#
# Exit codes:
#   0  all checks pass
#   1  jobs.json missing or unreadable
#   2  job `gecko-x-narrative-scanner` missing from jobs.json
#   3  `no_agent` is NOT `true`
#   4  `enabled` is NOT `true`
#   5  `script` path is empty or missing
#   6  required binary (jq, python3) missing
#
# Output: STRUCTURED stderr on any non-zero exit, suitable for piping to
# Telegram alerter or operator console. Stdout is empty on success unless
# --verbose specified.

set -uo pipefail

JOBS_JSON="${HERMES_CRON_JOBS_JSON:-/home/gecko-agent/.hermes/cron/jobs.json}"
JOB_ID="${HERMES_NARRATIVE_SCANNER_JOB_ID:-c849fffec986}"
QUIET=0
VERBOSE=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --quiet) QUIET=1; shift ;;
        --verbose) VERBOSE=1; shift ;;
        *) echo "ERROR: unknown arg: $1" >&2; exit 1 ;;
    esac
done

emit() {
    # Emit structured JSON to stderr. Operator can pipe to Telegram.
    local event="$1"
    shift
    local payload
    payload=$(python3 -c "
import json, sys
rec = {'event': '$event'}
for kv in sys.argv[1:]:
    k, _, v = kv.partition('=')
    rec[k] = v
print(json.dumps(rec))
" "$@" 2>/dev/null || echo "{\"event\":\"$event\",\"emit-fallback\":\"true\"}")
    echo "$payload" >&2
}

# Bootstrap — check required binaries
if ! command -v jq >/dev/null 2>&1; then
    echo "ERROR: jq binary not found" >&2
    exit 6
fi
if ! command -v python3 >/dev/null 2>&1; then
    echo "ERROR: python3 binary not found" >&2
    exit 6
fi

# Check 1: jobs.json exists + readable
if [[ ! -r "$JOBS_JSON" ]]; then
    emit "HERMES-NO-AGENT-CHECK-FAIL" "reason=jobs-json-not-readable" "path=$JOBS_JSON"
    exit 1
fi

# Check 2: the job exists
JOB_JSON=$(jq -r --arg id "$JOB_ID" '.jobs[] | select(.id == $id)' "$JOBS_JSON" 2>/dev/null)
if [[ -z "$JOB_JSON" ]]; then
    emit "HERMES-NO-AGENT-CHECK-FAIL" "reason=job-not-found" "job-id=$JOB_ID" "path=$JOBS_JSON"
    exit 2
fi

# Check 3: no_agent == true
NO_AGENT=$(echo "$JOB_JSON" | jq -r '.no_agent')
if [[ "$NO_AGENT" != "true" ]]; then
    emit "HERMES-NO-AGENT-CHECK-FAIL" \
        "reason=no-agent-flag-flipped" \
        "expected=true" \
        "actual=$NO_AGENT" \
        "job-id=$JOB_ID" \
        "implication=prompt-injection-scanner-may-block-future-runs"
    exit 3
fi

# Check 4: enabled == true
ENABLED=$(echo "$JOB_JSON" | jq -r '.enabled')
if [[ "$ENABLED" != "true" ]]; then
    emit "HERMES-NO-AGENT-CHECK-FAIL" \
        "reason=cron-disabled" \
        "expected=true" \
        "actual=$ENABLED" \
        "job-id=$JOB_ID"
    exit 4
fi

# Check 5: script path present (non-empty)
SCRIPT_PATH=$(echo "$JOB_JSON" | jq -r '.script')
if [[ -z "$SCRIPT_PATH" || "$SCRIPT_PATH" == "null" ]]; then
    emit "HERMES-NO-AGENT-CHECK-FAIL" \
        "reason=script-path-missing" \
        "job-id=$JOB_ID"
    exit 5
fi

# All checks passed
if [[ "$VERBOSE" == "1" ]]; then
    SCHEDULE=$(echo "$JOB_JSON" | jq -r '.schedule.expr')
    LAST_STATUS=$(echo "$JOB_JSON" | jq -r '.last_status')
    echo "HERMES-NO-AGENT-CHECK-OK job-id=$JOB_ID no_agent=true enabled=true script=$SCRIPT_PATH schedule=$SCHEDULE last_status=$LAST_STATUS"
fi
exit 0
