#!/usr/bin/env bash
set -euo pipefail

services=(
  hermes-gateway
  codex-autonomous-dev-srilu
  codex-fleet-operator-brief
  codex-fleet-telegram-status
  codex-flyer-autodev-main
  codex-overnight-autonomy-main
  codex-overnight-autonomy-vpin
  codex-production-push-loop-main
  codex-production-push-loop-vpin
  codex-readonly-operator-brief
  codex-weekly-hygiene-main
  codex-weekly-hygiene-vpin
  gecko-dashboard
  gecko-pipeline
  nginx
  shift-agent-cockpit
)

echo "**Status**"
printf 'checked_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
printf 'hostname=%s\n' "$(hostname -f 2>/dev/null || hostname)"

failed_units="$(systemctl --failed --no-legend --plain 2>/dev/null | awk 'NF {print $1}')"
failed_count="$(printf '%s\n' "$failed_units" | awk 'NF {c++} END {print c+0}')"
printf 'failed_units=%s\n' "$failed_count"
if [ "$failed_count" != "0" ]; then
  printf 'failed_unit_list=%s\n' "$(printf '%s\n' "$failed_units" | paste -sd, -)"
fi

for svc in "${services[@]}"; do
  if systemctl list-unit-files "${svc}.service" >/dev/null 2>&1; then
    printf '%s=' "$svc"
    systemctl is-active "$svc" 2>/dev/null || true
  fi
done

printf 'codex_timers='
systemctl list-timers 'codex-*' --all --no-pager --no-legend 2>/dev/null \
  | awk 'NF {c++} END {print c+0}'
df -h / | awk 'NR==2{print "disk="$5" used,"$4" free"}'

echo "**Risks**"
if [ "$failed_count" = "0" ]; then
  echo "none"
else
  printf 'failed systemd units: %s\n' "$(printf '%s\n' "$failed_units" | paste -sd, -)"
fi
