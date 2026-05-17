#!/usr/bin/env bash
# cron/deploy.sh — idempotent crontab merge between sentinels
#
# BL-NEW-OTHER-PROD-CONFIG-AUDIT (cycle 11). Mirrors cycle-6's systemd/
# pattern: repo-tracked source-of-truth for the operator's crontab, with
# a deploy script that preserves any operator-added entries OUTSIDE the
# managed block.
#
# V54 fold:
#   (1) `matched=1` set inside /BEGIN/ rule so END guard works correctly.
#       Without it, subsequent deploys would APPEND a second fragment
#       copy (re-runs grow linearly).
#   (2) Tempfile staging via mktemp + trap so `crontab` install is
#       atomic; pipe-to-`crontab -` could partial-install on awk mid-
#       stream failure.

set -euo pipefail

REPO_DIR="${GECKO_REPO:-/root/gecko-alpha}"
FRAGMENT="$(cat "$REPO_DIR/cron/gecko-alpha.crontab")"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

crontab -l 2>/dev/null \
    | awk -v fragment="$FRAGMENT" '
        /^# === BEGIN gecko-alpha managed block/ { skip=1; matched=1; print fragment; next }
        /^# === END gecko-alpha managed block/ { skip=0; next }
        !skip
        END { if (!matched) { print fragment } }
    ' \
    > "$TMP"

crontab "$TMP"
echo "OK: gecko-alpha cron block updated"
crontab -l || true   # guard empty-crontab nonzero exit under set -e
