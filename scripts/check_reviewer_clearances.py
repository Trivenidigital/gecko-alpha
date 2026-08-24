#!/usr/bin/env python3
"""Detect a reviewer clearance that has gone stale against the tree it covered.

WHAT THIS IS, AND WHAT IT IS NOT
================================

This is a **lapse detector**. It catches the failure that actually happened on
PR #559: a clearance was recorded, production then moved underneath it, and the
merge report went on asserting the clearance still held. Recomputing rather
than re-reading is the whole mechanism.

It is **NOT an independence mechanism, and cannot be made into one here.** Two
things would be required, and neither exists in this repository today:

1. **Branch protection.** With none configured, no CI check can make anything
   unmergeable -- `mergeStateStatus` reads `UNSTABLE` (checks red, merge
   permitted) rather than `BLOCKED`. Verified against this repo: PR #560 was
   mergeable while this very check was failing.
2. **A review record outside the author's reach.** `.reviewers.toml` is
   author-writable, so repointing every clearance at the head is a four-line
   edit that turns this green -- and it makes step 3 vacuous, because the
   clearance tree is then compared against itself. GitHub review approvals
   would supply an unfakeable, commit-bound record, but this project has
   **zero** of them on every recent PR: its independent reviewers are agents
   whose verdicts live in session transcripts, not in the GitHub review API.

So a motivated author bypasses this trivially. That is documented here rather
than dressed up, because a check advertised as enforcement that is really a
reminder is worse than an honest reminder. What it does buy is the accidental
case, which is the one with a track record.

Both gaps are operator decisions and are recorded in
`docs/runbook_recompute_coverage.md`.

WHAT IT CHECKS
==============

The question is *"does the delta THIS pull request introduces in watched paths
carry a clearance?"* -- not *"is some historical SHA still current?"*. So:

* If the watched trees are identical between the **merge base** and the head,
  the PR introduces no production delta and **no clearance is required**. A
  documentation-only PR passes without touching the declaration at all.

  **This narrows the treadmill; it does not remove it.** `watch` includes
  `tests` and `.github`, so essentially every substantive PR has a watched
  delta and still lands red until someone writes SHAs into this file by hand --
  and for exactly the PRs where review matters most. The rubber-stamp pressure
  named above is unchanged for those. Only the docs-only case is genuinely
  exempt.

  This matters more than it looks. An earlier draft demanded a clearance from
  every PR unconditionally. Because this repo squash-merges -- which discards
  branch commits, so recorded SHAs stop being ancestors of master the moment a
  PR lands -- every new branch inherited a red check and had to edit the record
  to clear it. That manufactures exactly the rubber-stamp habit the check
  exists to prevent, and trains everyone to ignore a permanently red signal.
* Otherwise each required vector must have a clearance that is an ancestor of
  the head and whose watched trees match the head's.

THREE STEPS, IN ORDER
=====================

1. **A CALLER OBLIGATION, not a step this script performs:** the base ref must
   be current. This script issues no `git fetch` -- grep it, there is none --
   because the fetch belongs where the network is, and it lives in
   `.github/workflows/test.yml` un-swallowed so a failure there fails the job.
   Off-CI a stale `origin/master` moves the merge-base earlier, which
   *overstates* the delta and so fails CLOSED; that is the safe direction, but
   it is luck, not a guarantee this script provides. (A local `master` pinned
   60 commits back once inflated a branch from 36 files to 91 -- a failure that
   flatters, because it reads as diligence rather than as an error.)
2. `git merge-base --is-ancestor` -- the ancestry guard. It catches "wrong
   branch named" and is silent on "right branch, stale ref"; only step 1 closes
   that. A commit existing is not a commit being reachable.
3. Compare **tree hashes** of the watched paths, never `--name-only`, which
   misses a file restored to the wrong baseline.

Exit codes: 0 pass -- 1 a declaration or clearance problem (missing, narrowed,
lapsed, malformed, unresolvable watch entry) -- 2 base/head could not be
resolved. Everything that can be decided fails CLOSED as 1; only "I could not
work out what to compare" is 2.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

#: Script-relative, not CWD-relative: run from a subdirectory, a CWD-relative
#: path reports "declaration missing" instead of a usage error. `REVIEWERS_DECL`
#: overrides it so the test-suite can drive the real script against a purpose-
#: built repository rather than against this one.
DECL = Path(
    os.environ.get("REVIEWERS_DECL")
    or Path(__file__).resolve().parents[1] / ".reviewers.toml"
)

#: Ruling D's four vectors, pinned in EXECUTABLE code rather than only in a
#: test, because a declaration that can narrow `required` to one vector can
#: equally narrow the test that checks it.
MANDATORY_VECTORS = frozenset({"concurrency", "silent-failure", "ops-safety", "logic"})

#: Paths whose movement must be covered by a clearance. `.reviewers.toml` can
#: never appear here -- a file cannot police edits to itself -- and that is the
#: clearest tell that a committed file is the wrong home for this record.
#: `dashboard` is here because that is where the /health WAL-sidecar silent
#: failure lived, and `.github` because a PR deleting this step from the
#: workflow must not be able to lapse nothing.
MANDATORY_WATCH = frozenset(
    {
        "scout",
        "scripts",
        "tests",
        ".github",
        "dashboard",
        # The DEPLOY SUBSTRATE, added after the delta shortcut below changed
        # what this list means. While a clearance was demanded unconditionally,
        # an omission here only failed to LAPSE a clearance. Once "no delta in
        # watched paths" became a pass, every omission turned into a silent
        # full exemption -- announced cheerfully as "no clearance required".
        #
        # These three are not academic for this project: a 2-minute `cron`
        # entry once migrated the live database into 74 `database is locked`
        # errors, and `systemd` units run scripts from `/usr/local/bin` copies,
        # so `git pull` deploys nothing and a unit edit is exactly the delta a
        # reviewer needs to see.
        "cron",
        "ops",
        "systemd",
        # Root FILES. An earlier draft of this list claimed they could not be
        # covered "because a path prefix cannot reach a root file", and
        # documented that as a known gap. That was simply wrong:
        # `git rev-parse <rev>:pyproject.toml` returns a BLOB hash, and
        # comparing blob hashes is the same operation as comparing tree hashes.
        # The gap was an artifact of not testing the claim.
        #
        # `uv.lock` and `pyproject.toml` reach production through `uv sync` on
        # the next restart -- a documented crash-loop surface here -- and the
        # container/entrypoint files decide what actually runs.
        "pyproject.toml",
        "uv.lock",
        "Dockerfile",
        "docker-compose.yml",
        "start.sh",
    }
)

_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")


def _git(*args: str) -> str:
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def _tree(rev: str, path: str) -> str | None:
    try:
        return _git("rev-parse", f"{rev}:{path}")
    except RuntimeError:
        return None


def _moved(watch: list[str], a: str, b: str) -> tuple[list[str], list[str]]:
    """Watched paths differing between `a` and `b`, and those absent at BOTH.

    The second list is why this returns a pair. `_tree` yields None for a path
    that does not exist, and `None != None` is False -- so a misspelled or
    renamed watch entry compared equal, was reported as covered, and printed the
    typo in the banner as though it were being watched. That was the only way
    this script could fail OPEN. It is now fatal rather than quiet.
    """
    moved = [p for p in watch if _tree(a, p) != _tree(b, p)]
    missing = [p for p in watch if _tree(a, p) is None and _tree(b, p) is None]
    return moved, missing


def main(argv: list[str]) -> int:
    head = argv[1] if len(argv) > 1 else "HEAD"
    base = argv[2] if len(argv) > 2 else "origin/master"

    if not DECL.exists():
        print(f"FAIL: declaration is missing: {DECL}")
        return 1
    try:
        decl = tomllib.loads(DECL.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        print(f"FAIL: {DECL} is not valid TOML: {exc}")
        return 1

    required = list(decl.get("required") or [])
    watch = list(decl.get("watch") or [])
    clearances = dict(decl.get("clearances") or {})

    narrowed = MANDATORY_VECTORS - set(required)
    if narrowed:
        print(
            "FAIL: required vectors narrowed -- missing "
            f"{', '.join(sorted(narrowed))}. Ruling D's four vectors are not a "
            "per-PR choice."
        )
        return 1
    unwatched = MANDATORY_WATCH - set(watch)
    if unwatched:
        print(f"FAIL: watch list narrowed -- missing {', '.join(sorted(unwatched))}.")
        return 1

    try:
        head_sha = _git("rev-parse", "--verify", "--quiet", f"{head}^{{commit}}")
        merge_base = _git("merge-base", base, head_sha)
    except RuntimeError as exc:
        print(f"FAIL: cannot resolve {head} against {base}: {exc}")
        return 2

    print(f"decl:  {DECL}")
    print(f"base:  {merge_base[:8]} (merge-base with {base})")
    print(f"head:  {head_sha[:8]}")
    print(f"watch: {', '.join(watch)}")

    delta, absent_both = _moved(watch, merge_base, head_sha)
    if absent_both:
        # Two different situations reach here, and telling the operator which
        # one they are in is the whole value of the message. The check itself
        # cannot distinguish them -- both look like "resolves nowhere" -- so it
        # names the remedy for each instead of guessing.
        #
        # A path pinned in MANDATORY_WATCH cannot be cleared by editing the
        # declaration: the floor above re-adds it. If such a path is
        # legitimately retired, EVERY subsequent PR hard-fails until the code
        # is changed -- which manufactures the permanently-red signal this
        # design exists to avoid. That is deliberate rather than accidental
        # (retiring a watched surface should be a reviewed code change), but a
        # message that does not say so reads as an unfixable bug.
        pinned = sorted(set(absent_both) & MANDATORY_WATCH)
        declared_only = sorted(set(absent_both) - MANDATORY_WATCH)
        print(
            f"\nFAIL: watched path(s) {', '.join(absent_both)} exist at neither "
            f"{merge_base[:8]} nor {head_sha[:8]} -- a watch entry that resolves "
            "nowhere monitors nothing while reporting coverage."
        )
        if declared_only:
            print(
                f"  {', '.join(declared_only)}: declared in .reviewers.toml only. "
                "Almost certainly a typo -- fix the name there."
            )
        if pinned:
            print(
                f"  {', '.join(pinned)}: pinned in MANDATORY_WATCH, so editing "
                ".reviewers.toml CANNOT clear this. If the path was legitimately "
                "retired, remove it from MANDATORY_WATCH in "
                "scripts/check_reviewer_clearances.py -- a code change, which is "
                "itself reviewed. Until then every PR fails here."
            )
        return 1

    # Validate the SHAPE of anything recorded, even when nothing is demanded.
    # Otherwise a malformed entry -- wrong length, or not a commit -- sits
    # unnoticed through every docs-only PR and then fails all four vectors at
    # once on the first PR that touches production, which is the least
    # convenient moment to discover a typo.
    shape_errors: list[str] = []
    for vector, sha in sorted(clearances.items()):
        if not _SHA_RE.match(str(sha)):
            shape_errors.append(f"{vector:16s} {sha!r} is not a full 40-hex SHA")
            continue
        try:
            _git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
        except RuntimeError:
            shape_errors.append(
                f"{vector:16s} {str(sha)[:8]} is not a commit in this repo"
            )
    if shape_errors:
        print("\nrecorded clearances are malformed:\n")
        for e in shape_errors:
            print(f"  {e}")
        return 1

    if not delta:
        print("\nno delta in watched paths; no clearance required.")
        return 0

    print(f"\ndelta in watched paths: {', '.join(delta)}")

    failures: list[str] = []
    for vector in sorted(required):
        sha = clearances.get(vector)
        if not sha:
            failures.append(f"{vector:16s} NO CLEARANCE RECORDED")
            continue
        if not _SHA_RE.match(str(sha)):
            failures.append(
                f"{vector:16s} {sha!r} IS NOT A FULL 40-HEX SHA -- a branch or "
                "tag silently re-points as it moves, so it cannot record which "
                "revision was reviewed"
            )
            continue
        try:
            # `_git` raises on a nonzero exit, and `--verify --quiet` never
            # exits 0 with empty stdout, so no emptiness check is needed here.
            full = _git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
        except RuntimeError:
            failures.append(
                f"{vector:16s} {str(sha)[:8]} IS NOT A COMMIT IN THIS REPO"
            )
            continue
        anc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", full, head_sha],
            capture_output=True,
            text=True,
        )
        if anc.returncode != 0:
            failures.append(
                f"{vector:16s} {str(sha)[:8]} IS NOT AN ANCESTOR of {head_sha[:8]}"
            )
            continue
        moved, _ = _moved(watch, full, head_sha)
        if moved:
            failures.append(
                f"{vector:16s} LAPSED -- {', '.join(moved)} moved since {str(sha)[:8]}"
            )
        else:
            print(f"  {vector:16s} HOLDS at {sha[:8]}")

    if failures:
        print("\nreviewer-clearance check FAILED:\n")
        for f in failures:
            print(f"  {f}")
        print(
            "\nA clearance is a property of a revision, not of a branch. It is "
            "not carried forward across a production delta."
        )
        return 1

    print("\nall required vectors hold at this head.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
