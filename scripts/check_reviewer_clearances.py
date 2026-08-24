#!/usr/bin/env python3
"""Fail the build when a required independent-review clearance is missing or lapsed.

Ruling D says a merge candidate needs an independent clearance per required
vector, and that a clearance is a property of a REVISION, not of a branch or a
component. On PR #559 that rule worked -- two slots were sent back after
`scout/db.py` moved, and both re-runs produced findings that did not exist at
the clearances that would otherwise have been carried forward.

But the rule did not catch that. A person did, at the moment when accepting an
already-held clearance was cheapest. That is discipline under observation with
four reviewers watching, not a mechanism, and the merge report's own §7 argues
against exactly that shape: prefer the version of a virtue that has a mechanism
attached. This is the mechanism.

The declaration lives in `.reviewers.toml` at the repo root:

    required = ["concurrency", "silent-failure", "ops-safety", "logic"]
    watch    = ["scout", "scripts"]

    [clearances]
    concurrency    = "f7a200cf"
    silent-failure = "f7a200cf"

Exit codes: 0 pass · 1 lapsed/missing/invalid clearance · 2 usage or git error.

THREE STEPS, IN ORDER. The first was missing until it cost us:

1. `git fetch` -- the base ref itself may be stale. A local `master` pinned 60
   commits back inflated a branch from 36 files to 91 and made it look like it
   carried other PRs' work. That failure OVERSTATES footprint, so it reads as
   diligence rather than as an error.
2. `git merge-base --is-ancestor` -- the ancestry guard. Without it,
   `origin/HEAD` resolving to `origin/master` produced a false LAPSED reporting
   3,546 deletions. Step 2 catches "wrong branch named" and is SILENT on "right
   branch, stale ref"; only step 1 closes that.
3. Compare TREE HASHES of the watched paths, not `--name-only`. Name-level
   diffing misses a file restored to the wrong baseline.

And a commit existing is not a commit being reachable: ancestry is evaluated
from the revision under review, never from whatever was last pushed.

RUNS ON `pull_request` ONLY, and that is not a preference. This repo
squash-merges, which discards the branch commits -- so after the merge the
clearance SHAs are no longer ancestors of anything on master, and the gate
would fail every push. Found by running it against `cdbb8475` immediately after
#559 landed:

    git merge-base --is-ancestor f7a200cf cdbb8475   -> NO   (squashed away)
    git merge-base --is-ancestor f7a200cf e2e3440a   -> YES  (the PR head)

The revision under review is the PR head, which is exactly where the question
"would this merge with an unreviewed delta?" is meaningful. Verify a
squash-merge landed by CONTENT (tree equality), not by ancestry.
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path

DECL = Path(".reviewers.toml")


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def _tree(rev: str, path: str) -> str | None:
    """Tree hash of `path` at `rev`, or None when the path does not exist there."""
    try:
        return _git("rev-parse", f"{rev}:{path}")
    except RuntimeError:
        return None


def main(argv: list[str]) -> int:
    head = argv[1] if len(argv) > 1 else "HEAD"

    if not DECL.exists():
        print(f"FAIL: {DECL} is missing -- every merge candidate must declare "
              f"its required review vectors and the SHA each was cleared on.")
        return 1

    try:
        decl = tomllib.loads(DECL.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        print(f"FAIL: {DECL} is not valid TOML: {exc}")
        return 2

    required = decl.get("required") or []
    watch = decl.get("watch") or ["scout", "scripts"]
    clearances = decl.get("clearances") or {}

    if not required:
        print(f"FAIL: {DECL} declares no required vectors.")
        return 1

    # Step 1. Without this the ancestry guard below is silent on a stale ref.
    try:
        subprocess.run(["git", "fetch", "--quiet", "origin"], check=False,
                       capture_output=True, text=True, timeout=120)
    except Exception:
        pass  # offline runners still get steps 2 and 3

    try:
        head_sha = _git("rev-parse", head)
    except RuntimeError as exc:
        print(f"FAIL: cannot resolve {head}: {exc}")
        return 2

    print(f"head under review: {head_sha[:8]}")
    print(f"watched paths:     {', '.join(watch)}")

    failures: list[str] = []
    for vector in required:
        sha = clearances.get(vector)
        if not sha:
            failures.append(f"{vector:16s} NO CLEARANCE RECORDED")
            continue

        try:
            # `--verify <sha>^{commit}` and not a bare `rev-parse`: a bare
            # rev-parse happily echoes any 40-hex string back without checking
            # the object exists, so an unknown SHA fell through to the ancestry
            # check and was reported as "not an ancestor" -- a true statement
            # about a commit that is not in the repository at all, and a
            # misleading one to debug. Found by the test that asserts the
            # REASON and not only the exit code.
            full = _git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
            if not full:
                raise RuntimeError("no such commit")
        except RuntimeError:
            failures.append(f"{vector:16s} {sha} IS NOT A COMMIT IN THIS REPO")
            continue

        # Step 2. A non-ancestor reports invalid rather than a spurious revert.
        anc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", full, head_sha],
            capture_output=True, text=True,
        )
        if anc.returncode != 0:
            failures.append(
                f"{vector:16s} {sha[:8]} IS NOT AN ANCESTOR of {head_sha[:8]} "
                f"-- the clearance was measured on a revision this head does "
                f"not contain, so it says nothing about what would merge"
            )
            continue

        # Step 3. Tree hashes, not names.
        moved = [p for p in watch if _tree(full, p) != _tree(head_sha, p)]
        if moved:
            failures.append(
                f"{vector:16s} LAPSED -- {', '.join(moved)} moved since "
                f"{sha[:8]}; re-run this vector against {head_sha[:8]}"
            )
        else:
            print(f"  {vector:16s} HOLDS at {sha[:8]}")

    if failures:
        print("\nreviewer-clearance gate FAILED:\n")
        for f in failures:
            print(f"  {f}")
        print(
            "\nA clearance is a property of a revision, not of a branch. It is "
            "not carried forward across a production delta, and the "
            "implementer's own review never satisfies an independent slot."
        )
        return 1

    print("\nall required vectors hold at this head.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
