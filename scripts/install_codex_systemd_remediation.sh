#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

install -m 0755 "$repo_root/scripts/codex_systemd_failure_alert.py" /usr/local/bin/codex-systemd-failure-alert
install -m 0755 "$repo_root/scripts/codex_systemd_auto_remediate.py" /usr/local/bin/codex-systemd-auto-remediate

install -m 0644 "$repo_root/systemd/codex-systemd-failure-alert@.service" /etc/systemd/system/codex-systemd-failure-alert@.service
install -m 0644 "$repo_root/systemd/codex-systemd-auto-remediate@.service" /etc/systemd/system/codex-systemd-auto-remediate@.service

install -d -o root -g root -m 0755 /run/codex-remediation /var/lib/codex-remediation
touch /var/log/codex-remediation.log
chmod 0644 /var/log/codex-remediation.log

for unit in "$@"; do
  case "$unit" in
    codex-systemd-failure-alert@*.service|codex-systemd-auto-remediate@*.service)
      echo "refusing to attach OnFailure to handler unit: $unit" >&2
      exit 2
      ;;
  esac
  install -d -o root -g root -m 0755 "/etc/systemd/system/${unit}.d"
  install -m 0644 "$repo_root/systemd/codex-telegram-onfailure.conf" "/etc/systemd/system/${unit}.d/10-telegram-onfailure.conf"
done

systemctl daemon-reload
