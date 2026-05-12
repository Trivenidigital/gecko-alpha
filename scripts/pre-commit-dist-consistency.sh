#!/usr/bin/env bash
# Pre-commit hook: enforce dist/ consistency between index.html and assets.
#
# Prevents the silent-failure shape from 2026-05-12 (commit dce4e87 fix):
# dist/assets/*.js gets rebuilt with a new content hash, but
# dist/index.html isn't staged alongside, so the deployed page references
# a dead asset hash and the browser hits 404 on first load.
#
# Root cause of the original incident: dist/index.html was on
# --skip-worktree, hiding "this file is dirty" from `git status`. The hack
# was removed in dce4e87, but the residual human-error case remains:
# operator runs `npm run build`, stages the new asset file with `git add`,
# forgets to also stage the updated index.html. This hook catches that.
#
# The check is consistency-based, not presence-based: parse asset refs
# from the (staged or current) index.html, verify each ref exists in the
# staged tree. Catches three failure modes:
#   1. Assets staged without index.html (today's case)
#   2. Index.html staged with refs to assets that no longer exist
#   3. Deletion of an asset still referenced by index.html
#
# Bypass: `git commit --no-verify` (the hook respects --no-verify by
# design; emergency commits are a real use case and shouldn't be blocked).

set -euo pipefail

DIST_HTML="dashboard/frontend/dist/index.html"
DIST_ASSETS_PREFIX="dashboard/frontend/dist/assets/"

# Files staged for ADD or MODIFY in this commit
staged_files="$(git diff --cached --name-only --diff-filter=AM 2>/dev/null || true)"
# Files staged for DELETE
removed_files="$(git diff --cached --name-only --diff-filter=D 2>/dev/null || true)"

# If no dist/ files are touched in this commit, skip the check entirely.
# The hook is scoped to dist/ consistency only; it doesn't moralize about
# the rest of the staged set.
dist_touched=0
if printf '%s\n' "$staged_files" | grep -q "^dashboard/frontend/dist/"; then
    dist_touched=1
fi
if printf '%s\n' "$removed_files" | grep -q "^dashboard/frontend/dist/"; then
    dist_touched=1
fi
if (( dist_touched == 0 )); then
    exit 0
fi

# Determine which version of index.html to read:
# - If index.html is in the staged set, use the staged version (git show :path)
# - Else, use the current working tree (which equals HEAD)
# - If neither exists, that's a hard error
if printf '%s\n' "$staged_files" | grep -qx "$DIST_HTML"; then
    html_content="$(git show ":${DIST_HTML}" 2>/dev/null || true)"
elif printf '%s\n' "$removed_files" | grep -qx "$DIST_HTML"; then
    echo "pre-commit-dist-consistency: ERROR — $DIST_HTML is being deleted but dist/ files remain staged. Refusing." >&2
    exit 1
elif [[ -f "$DIST_HTML" ]]; then
    html_content="$(cat "$DIST_HTML")"
else
    echo "pre-commit-dist-consistency: ERROR — $DIST_HTML missing from both staged tree and working tree." >&2
    exit 1
fi

# Extract asset references (e.g., /assets/index-ABC123.js) from index.html.
# Pattern matches Vite's output shape: /assets/<name>.<ext> where ext is js/css.
asset_refs="$(printf '%s' "$html_content" \
    | grep -oE '/assets/[A-Za-z0-9_.-]+\.(js|css)' \
    | sort -u || true)"

if [[ -z "$asset_refs" ]]; then
    # index.html has no asset refs — unusual but not necessarily wrong
    # (e.g., a minimal placeholder). Pass; the hook only enforces consistency
    # of refs that exist.
    exit 0
fi

# For each asset ref, verify the corresponding file exists in the staged tree.
# "Exists in staged tree" means one of:
#   - Already tracked AND not in the removed_files set
#   - Being added in this commit (in staged_files)
missing=()
for ref in $asset_refs; do
    # Strip leading slash, prefix with dist root
    rel_path="${ref#/}"  # e.g., assets/index-ABC.js
    asset_path="dashboard/frontend/dist/${rel_path}"

    if printf '%s\n' "$removed_files" | grep -qx "$asset_path"; then
        # Asset is being deleted in this commit but index.html still refs it
        missing+=("$asset_path (deleted in this commit but still referenced by index.html)")
        continue
    fi

    # Check if it's already tracked (and not being deleted)
    if git ls-files --error-unmatch "$asset_path" >/dev/null 2>&1; then
        # Tracked, not deleted — consistent
        continue
    fi

    # Not tracked — must be in the staged-add set
    if printf '%s\n' "$staged_files" | grep -qx "$asset_path"; then
        continue
    fi

    missing+=("$asset_path (referenced by index.html but not in staged tree)")
done

if (( ${#missing[@]} > 0 )); then
    {
        echo ""
        echo "pre-commit-dist-consistency: REFUSING COMMIT"
        echo ""
        echo "$DIST_HTML references asset(s) not present in the staged tree:"
        echo ""
        for m in "${missing[@]}"; do
            echo "  - $m"
        done
        echo ""
        echo "This typically happens when:"
        echo "  * dist/assets/ was rebuilt locally, but $DIST_HTML wasn't"
        echo "    staged with the new asset hashes"
        echo "  * $DIST_HTML was committed with stale asset-hash references"
        echo ""
        echo "To fix:"
        echo "  1. Rebuild: (cd dashboard/frontend && npm run build)"
        echo "  2. Stage updated dist/: git add dashboard/frontend/dist/"
        echo "  3. Re-attempt the commit"
        echo ""
        echo "Emergency bypass (deploy will likely break):"
        echo "  git commit --no-verify"
        echo ""
    } >&2
    exit 1
fi

exit 0
