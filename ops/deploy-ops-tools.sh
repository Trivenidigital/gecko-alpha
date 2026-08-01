#!/usr/bin/env bash
# Deploy Gecko ops tools from the repo to /usr/local/bin.
#
# The repo is the source of truth. These tools must never exist only as
# unmanaged files on a VPS — that is how a host silently diverges from what
# review approved. Idempotent: re-running verifies rather than re-copies.
#
# Usage:  sudo ops/deploy-ops-tools.sh [--check]
#   --check  verify deployed copies match the repo and exit non-zero on drift;
#            makes no changes. Suitable for a cron drift check.
set -euo pipefail

DEST=/usr/local/bin
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS=(gecko-solana-verify)

CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

rc=0
for tool in "${TOOLS[@]}"; do
  src="$SRC_DIR/$tool"
  dst="$DEST/$tool"

  if [ ! -f "$src" ]; then
    printf 'MISSING IN REPO: %s\n' "$src" >&2
    rc=1
    continue
  fi

  want=$(sha256sum "$src" | cut -d' ' -f1)
  have=""
  [ -f "$dst" ] && have=$(sha256sum "$dst" | cut -d' ' -f1)

  if [ "$want" = "$have" ] && [ "$(stat -c%a "$dst")" = "755" ]; then
    printf 'OK      %s (%s)\n' "$tool" "${want:0:16}"
    continue
  fi

  if [ "$CHECK_ONLY" = 1 ]; then
    printf 'DRIFT   %s: repo=%s deployed=%s\n' \
      "$tool" "${want:0:16}" "${have:0:16}$([ -z "$have" ] && echo '(absent)')" >&2
    rc=1
    continue
  fi

  install -m 755 "$src" "$dst"
  printf 'DEPLOY  %s (%s)\n' "$tool" "${want:0:16}"
done

exit $rc
