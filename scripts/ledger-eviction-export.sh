#!/usr/bin/env bash
# Weekly export of ledger_enrollment_evicted journal records to a durable
# append-only JSONL file — interim durability until BL-NEW-LEDGER-EVICTION-DB-MARKER
# lands (the journal is size-rotated; this file is the idempotent copy).
# Output: journald envelope per line (-o json); the structlog event is .MESSAGE.
# DEDUP AT READ TIME on the STRUCTLOG fields (event-time, stable across runs):
#   jq '.MESSAGE | fromjson | [.timestamp, .evicted_token_ids]'
# — never on journald arrival fields.
# Failure visibility (detection-must-reach-a-human): any error triggers a
# plain-text Telegram alert via the env-token urllib pattern
# (gecko-backup-watchdog.sh convention — token NOT in argv), plus the cron log.
# appended=0 is a NORMAL outcome (no cap pressure that week), not a failure.
# Runbook: docs/runbook_deploy_ga_fixes_2026_07_02.md (deploy-#2 weekly steps).
set -euo pipefail
EXPORT_FILE="${LEDGER_EVICTION_EXPORT_FILE:-/var/lib/gecko-alpha/ledger_eviction_export.jsonl}"
ENV_FILE="${GECKO_ENV_FILE:-/root/gecko-alpha/.env}"

alert_failure() {
  local msg="ledger-eviction-export FAILED on $(hostname) at $(date -u +%FT%TZ) (line $1). Weekly eviction durability copy did not run - journal rotation clock is ticking. See /var/log/gecko-alpha-ledger-eviction-export.log"
  if [ -f "$ENV_FILE" ]; then
    set +e
    TG_TOKEN="$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | cut -d= -f2-)" \
    TG_CHAT="$(grep -E '^TELEGRAM_CHAT_ID=' "$ENV_FILE" | cut -d= -f2-)" \
    TG_MSG="$msg" python3 - <<'PYEOF2'
import json, os, urllib.request
tok, chat, msg = os.environ.get("TG_TOKEN",""), os.environ.get("TG_CHAT",""), os.environ.get("TG_MSG","")
if tok and chat:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{tok}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": msg}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
        print("ledger_eviction_export_failure_alert_delivered")
    except Exception as e:
        print(f"ledger_eviction_export_failure_alert_failed err={e}")
PYEOF2
    set -e
  fi
  echo "ledger_eviction_export_run status=FAILED line=$1"
}
trap 'alert_failure $LINENO' ERR

mkdir -p "$(dirname "$EXPORT_FILE")"
matches="$(journalctl -u gecko-pipeline --since '-8 days' -o json 2>/dev/null | grep ledger_enrollment_evicted || true)"
n=0
if [ -n "$matches" ]; then
  printf '%s\n' "$matches" >> "$EXPORT_FILE"
  n="$(printf '%s\n' "$matches" | wc -l)"
fi
echo "ledger_eviction_export_run status=ok appended=$n file=$EXPORT_FILE"
