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
        # Build-time discovery: existing srilu crontab has the 2 gecko
        # entries WITHOUT sentinel bracketing. The plain `!skip` rule
        # would preserve them, then END appends the fragment, producing
        # DUPLICATES. Strip any `/root/gecko-alpha/scripts/` line found
        # OUTSIDE the sentinel block — those belong inside the managed
        # block, which the fragment replaces atomically. Operator manual
        # entries pointing to other paths (polymarket, etc.) are preserved.
        !skip && /\/root\/gecko-alpha\/scripts\// { next }
        !skip
        END { if (!matched) { print fragment } }
    ' \
    > "$TMP"

crontab "$TMP"
echo "OK: gecko-alpha cron block updated"
crontab -l || true   # guard empty-crontab nonzero exit under set -e
