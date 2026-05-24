#!/usr/bin/env bash
# install_codex_auth_lock.sh — wrap codex CLI invocations on srilu in a
# shared flock at /run/codex.auth.lock so that codex-auth-guard,
# codex-autonomous-dev-srilu, codex-readonly-operator-brief, and any
# operator-interactive codex sessions cannot race on the one-time-use
# OAuth refresh_token stored in ~/.codex/auth.json.
#
# Surfaced by PR #242 (post-autodev fleet review) and PR #243 (SIGTERM
# fix). The race materialised on srilu-vps 2026-05-24T03:59:36Z when
# codex returned `401 Unauthorized: refresh_token_reused` and the
# autodev unit exited 2/INVALIDARGUMENT, firing the OnFailure Telegram
# chain. The next autodev run at 04:45Z self-healed.
#
# Approach: install a small shim at /usr/local/bin/codex-locked that
# acquires the flock then execs the real codex binary with all args.
# Operator THEN updates the existing /usr/local/bin/codex-auth-guard
# and /usr/local/bin/codex-autonomous-dev-srilu to call `codex-locked`
# in place of `codex`. This script automates step 1 and provides a
# guided sed-edit for step 2 (with diff preview + backup).
#
# Idempotent: re-running is safe; refuses to patch if scripts have
# diverged from the recorded baseline (operator must merge manually).
#
# Usage:
#   bash scripts/install_codex_auth_lock.sh         # dry-run, prints plan
#   bash scripts/install_codex_auth_lock.sh --apply # actually mutate
#
# Rollback:
#   The script creates /usr/local/bin/<name>.bak-<timestamp> for each
#   patched file. Restore with `cp <bak> <original>`.

set -Eeuo pipefail

CODEX_LOCK_PATH="/run/codex.auth.lock"
SHIM_PATH="/usr/local/bin/codex-locked"
TARGETS=(
    "/usr/local/bin/codex-auth-guard"
    "/usr/local/bin/codex-autonomous-dev-srilu"
)
APPLY=0
TS="$(date -u +%Y%m%dT%H%M%SZ)"

for arg in "$@"; do
    case "$arg" in
        --apply) APPLY=1 ;;
        -h|--help) sed -n '1,30p' "$0"; exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 2 ;;
    esac
done

# Pre-flight: confirm we're on the srilu VPS.
HOST="$(hostname -f 2>/dev/null || hostname)"
case "$(printf '%s' "$HOST" | tr '[:upper:]' '[:lower:]')" in
    *srilu*|ubuntu-*) ;;
    *)
        echo "REFUSE: not on srilu (hostname=$HOST). Run on the VPS." >&2
        exit 3
        ;;
esac

if [ "$EUID" -ne 0 ]; then
    echo "REFUSE: must run as root (to write /usr/local/bin)." >&2
    exit 4
fi

CODEX_BIN="$(command -v codex 2>/dev/null || true)"
if [ -z "$CODEX_BIN" ]; then
    echo "REFUSE: codex CLI not on PATH" >&2
    exit 5
fi
if [ "$CODEX_BIN" = "$SHIM_PATH" ]; then
    echo "REFUSE: codex resolves to the shim itself — would recurse" >&2
    exit 6
fi

# Step 1 plan: install codex-locked shim.
shim_content="$(cat <<EOF
#!/usr/bin/env bash
# codex-locked — serialise codex CLI on srilu via flock to prevent
# concurrent OAuth refresh_token rotation. Installed by
# scripts/install_codex_auth_lock.sh. Real codex binary: $CODEX_BIN.
set -Eeuo pipefail
exec flock --wait 60 "$CODEX_LOCK_PATH" "$CODEX_BIN" "\$@"
EOF
)"

step1_needed=1
if [ -f "$SHIM_PATH" ] && [ "$(cat "$SHIM_PATH")" = "$shim_content" ]; then
    step1_needed=0
fi

# Step 2 plan: each target script must reference `codex-locked` not bare
# `codex`. We refuse to patch automatically — operator must hand-edit so
# they have full visibility of which invocations got wrapped.
declare -a step2_targets_needing_edit=()
for target in "${TARGETS[@]}"; do
    if [ ! -f "$target" ]; then
        echo "WARN: target $target not present — skipping" >&2
        continue
    fi
    # Heuristic: a target is "done" if it contains the string codex-locked
    # at least as many times as it contains the bare command `codex `.
    locked_refs="$(grep -c "codex-locked" "$target" 2>/dev/null || echo 0)"
    bare_refs="$(grep -cE '(^|[^a-z-])codex (login|exec)' "$target" 2>/dev/null || echo 0)"
    if [ "$locked_refs" -lt "$bare_refs" ]; then
        step2_targets_needing_edit+=("$target")
    fi
done

# Report plan.
echo "== install_codex_auth_lock.sh plan =="
echo "host: $HOST"
echo "codex binary: $CODEX_BIN"
echo "lock path: $CODEX_LOCK_PATH"
echo
if [ "$step1_needed" -eq 1 ]; then
    echo "[step 1] install/refresh shim at $SHIM_PATH"
else
    echo "[step 1] shim at $SHIM_PATH already up to date"
fi
echo
if [ "${#step2_targets_needing_edit[@]}" -eq 0 ]; then
    echo "[step 2] no targets need editing — already routed via codex-locked"
else
    echo "[step 2] the following scripts still call bare 'codex'; operator must edit:"
    for target in "${step2_targets_needing_edit[@]}"; do
        echo "  - $target"
        printf '      replace: '\''codex (login|exec) ...'\'' → '\''codex-locked $1 ...'\''\n'
    done
fi
echo
if [ "$APPLY" -eq 0 ]; then
    echo "DRY RUN — re-run with --apply to execute step 1 (step 2 is always manual)"
    exit 0
fi

# Apply step 1.
if [ "$step1_needed" -eq 1 ]; then
    if [ -f "$SHIM_PATH" ]; then
        cp -a "$SHIM_PATH" "${SHIM_PATH}.bak-${TS}"
        echo "backup: ${SHIM_PATH}.bak-${TS}"
    fi
    printf '%s\n' "$shim_content" > "$SHIM_PATH"
    chmod 0755 "$SHIM_PATH"
    echo "installed: $SHIM_PATH"
fi

# Touch the lock file so subsequent flock calls don't race on creation.
install -m 0600 -o root -g root /dev/null "$CODEX_LOCK_PATH" 2>/dev/null || true

# Smoke: lock-acquire test.
if flock --wait 5 "$CODEX_LOCK_PATH" true; then
    echo "smoke: flock acquire/release ok"
else
    echo "ERROR: flock acquire failed — investigate before proceeding" >&2
    exit 7
fi

# Smoke: shim exec with a no-op (codex --version).
if "$SHIM_PATH" --version >/dev/null 2>&1; then
    echo "smoke: codex-locked --version ok"
else
    echo "WARN: codex-locked --version returned non-zero; check shim/codex compat" >&2
fi

echo
echo "[step 1 COMPLETE]"
if [ "${#step2_targets_needing_edit[@]}" -gt 0 ]; then
    echo "[step 2 still TODO] hand-edit the targets above to replace 'codex' with 'codex-locked'"
    echo "then verify with: grep -cE '(^|[^a-z-])codex (login|exec)' <target>"
fi
