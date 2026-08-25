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
2. **A review record outside the author's reach.** `.reviewers/<pr>.toml` is
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
  the PR introduces no production delta and **no clearance SHA is required**.

  It still needs its own correctly-owned `.reviewers/<pr>.toml`, and that is
  the executable rule -- the record is resolved and its ownership verified
  BEFORE the watched delta is computed. An earlier version of this paragraph
  said a docs-only PR "passes without touching the declaration at all", which
  described a weaker gate than the one that shipped. The stronger rule is
  deliberate: **known PR + missing active record = RED.** If absence were a
  pass, deleting the record would be the cheapest way to turn the gate green
  while switching it off -- and a docs-only PR is precisely where nobody would
  look twice.

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

import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

#: Root of the per-PR clearance records. Script-relative, not CWD-relative:
#: run from a subdirectory, a CWD-relative path reports "declaration missing"
#: instead of a usage error. `REVIEWERS_DIR` overrides it so the test-suite can
#: drive the real script against a purpose-built repository rather than this one.
DECL_DIR = Path(
    os.environ.get("REVIEWERS_DIR")
    or Path(__file__).resolve().parents[1] / ".reviewers"
)


class PRIdentityUnresolved(Exception):
    """The candidate PR could not be identified. DELIBERATELY NOT a failure to
    find a declaration.

    These are different facts and collapsing them is a fail-open: "there is no
    clearance for PR 42" is a verdict, while "I do not know which PR this is"
    is the absence of one. If the second ever resolves to the first, a run with
    no identity would report a specific, checkable-sounding reason and a future
    reader would go looking for a file rather than for the missing context.

    This is the same shape as `GitTimeout`, and it is here for the same reason:
    the previous design had ONE declaration for the whole repository, so there
    was no identity to get wrong. Introducing per-PR records introduces a new
    way to be wrong about WHICH record applies.
    """


def _parse_args(argv: list[str]) -> tuple[str, str, str]:
    """`(head, base, pr)` from a REAL parser, not positional slicing.

    The previous version read `argv[1]` as `head` and `argv[2]` as `base`
    before looking for `--pr` anywhere in the list. `--pr 564` on its own
    therefore resolved `head="--pr"` and `base="564"`, and failed with
    `cannot resolve --pr against 564` -- an option silently reinterpreted as a
    git ref. It was invisible in the test-suite because every call site passed
    `head base --pr N` in that exact order, so the slice happened to land
    correctly. A contract that only holds for one argument order is not a
    contract; it is a coincidence the tests were written around.

    PR identity precedence, and conflicts are FATAL rather than
    last-one-wins: silently preferring one of two disagreeing identities is
    how a run ends up evaluating a different PR than the operator believes.
    """
    ap = argparse.ArgumentParser(
        prog="check_reviewer_clearances",
        add_help=True,
        description="Verify this PR's reviewer clearances against its tree.",
    )
    ap.add_argument("head", nargs="?", default="HEAD")
    ap.add_argument("base", nargs="?", default="origin/master")
    ap.add_argument(
        "--pr",
        action="append",
        default=None,
        help="PR number. In CI supply it from github.event.pull_request.number.",
    )

    # `parse_args` exits 2 on a usage error, which is already the code this
    # script reserves for "could not work out what to compare". Kept rather
    # than intercepted: an unparseable command line is precisely that case.
    ns = ap.parse_args(argv[1:])

    given = list(ns.pr or [])
    if len(set(given)) > 1:
        raise PRIdentityUnresolved(
            "conflicting --pr values: " + ", ".join(sorted(set(given)))
            + " -- refusing to pick one"
        )
    env = os.environ.get("REVIEWERS_PR", "").strip()
    if given and env and given[0] != env:
        raise PRIdentityUnresolved(
            f"--pr {given[0]} disagrees with REVIEWERS_PR {env} -- refusing to "
            "pick one. One of them is describing a different PR than you think."
        )

    pr = given[0] if given else env
    if not pr:
        raise PRIdentityUnresolved(
            "no PR identity: pass `--pr <number>`, or set REVIEWERS_PR from "
            "trusted CI event metadata. The branch name is deliberately NOT "
            "consulted -- it is author-controlled and would let any branch "
            "claim any PR's clearances"
        )
    return ns.head, ns.base, pr


_PR_RE = re.compile(r"\A[1-9][0-9]{0,9}\Z")


def _decl_path(pr: str) -> Path:
    """`.reviewers/<pr>.toml`, with the PR number validated as a bare number.

    Validated rather than trusted: the value reaches a filesystem path, so
    `../../etc/passwd` or `42/../41` must not resolve to another PR's record or
    to anything outside `DECL_DIR`. A malformed number is an identity failure,
    not a missing declaration.
    """
    if not _PR_RE.match(pr):
        raise PRIdentityUnresolved(
            f"PR identity {pr!r} is not a bare positive number -- refusing to "
            "resolve it to a path"
        )
    return DECL_DIR / f"{pr}.toml"

#: Ruling D's four vectors, pinned in EXECUTABLE code rather than only in a
#: test, because a declaration that can narrow `required` to one vector can
#: equally narrow the test that checks it.
MANDATORY_VECTORS = frozenset({"concurrency", "silent-failure", "ops-safety", "logic"})

#: Paths whose movement must be covered by a clearance. `.reviewers/` can
#: never appear here -- a record cannot police edits to itself. Note this is a
#: DIFFERENT reason from the one the old root declaration had: that file was
#: excluded because a single shared table could not police its own rewriting;
#: a per-PR record is excluded because it is the evidence being evaluated.
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

#: Every `subprocess.run` in `scripts/` must pass an explicit timeout -- there is
#: a repo guard asserting it, and it caught this file. The reason applies here:
#: `git` can block indefinitely on a lock held by a concurrent process, and a
#: check that hangs is worse than one that fails, because a hung job reports
#: nothing at all.
_GIT_TIMEOUT_SEC = 60


class GitTimeout(Exception):
    """`git` did not answer. DELIBERATELY NOT a `RuntimeError`.

    This distinction is load-bearing and it was learned the hard way. When the
    timeout hardening landed, `TimeoutExpired` was converted into `RuntimeError`
    -- the same type `_git` raises for "that object does not exist". `_tree`
    catches `RuntimeError` and returns `None`, and `None` is `_moved`'s encoding
    for "this path is not present at that revision". So a timeout became
    indistinguishable from an absent path, and a reviewer demonstrated the
    consequence: with two transient timeouts against a repo where `scout/` had
    genuinely moved, `_moved` returned `([], [])` and the check printed
    "no delta in watched paths; no clearance required" and exited 0.

    A production change reported as needing no review, without touching the
    declaration -- the precise outcome this script exists to prevent, introduced
    by a fix for something else.

    One channel carrying two meanings. `_tree` was written when the only
    realistic failure of `rev-parse <rev>:<path>` was "no such path"; widening
    what `_git` raises without widening the handler is what did it.
    """


def _git(*args: str) -> str:
    try:
        r = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        raise GitTimeout(
            f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SEC}s"
        ) from None
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout.strip()


def _tree(rev: str, path: str) -> str | None:
    """Tree/blob hash of `path` at `rev`, or None when it does not exist there.

    `GitTimeout` is NOT caught: "git did not answer" is not "the path is
    absent", and collapsing the two is a fail-open. It propagates to `main`,
    which reports it and exits non-zero.
    """
    try:
        return _git("rev-parse", f"{rev}:{path}")
    except RuntimeError:
        return None


def _moved(watch: list[str], a: str, b: str) -> tuple[list[str], list[str]]:
    """Watched paths differing between `a` and `b`, and those absent at BOTH.

    The second list is why this returns a pair. `_tree` yields None for a path
    that does not exist, and `None != None` is False -- so a misspelled or
    renamed watch entry compared equal, was reported as covered, and printed the
    typo in the banner as though it were being watched.

    Two ways this could fail OPEN have now been found, and only one of them was
    the one above. The other was a timeout being routed through the same
    `RuntimeError` channel `_tree` uses to mean "absent" -- see `GitTimeout`.
    Both are closed; the earlier version of this docstring claimed the first was
    "the only way", which was true when written and false one commit later.

    Each path is resolved ONCE per revision. The previous version called `_tree`
    four times per path -- twice for `moved`, twice for `missing` -- so a
    transient failure could answer differently across the two comprehensions and
    produce a self-inconsistent verdict.
    """
    at_a = {p: _tree(a, p) for p in watch}
    at_b = {p: _tree(b, p) for p in watch}
    moved = [p for p in watch if at_a[p] != at_b[p]]
    missing = [p for p in watch if at_a[p] is None and at_b[p] is None]
    return moved, missing


def main(argv: list[str]) -> int:
    head, base, pr = _parse_args(argv)
    DECL = _decl_path(pr)

    if not DECL.exists():
        print(f"FAIL: no clearance recorded for PR {pr}: {DECL} does not exist")
        print(
            "  This is NOT a skip. A missing or renamed declaration for the "
            "PR under evaluation is RED, because the alternative -- treating "
            "absence as 'nothing to check' -- lets deleting the file turn the "
            "gate green while disabling it."
        )
        return 1
    try:
        decl = tomllib.loads(DECL.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        print(f"FAIL: {DECL} is not valid TOML: {exc}")
        return 1

    # The record must name its OWN PR, and it must match the identity resolved
    # from trusted metadata. Filename alone is not ownership: a file copied
    # from another PR keeps its contents, and a rename is a one-line edit. This
    # is what makes "PR A's clearance can never satisfy PR B" a property of the
    # DATA rather than of a naming convention.
    declared = decl.get("pr")
    if declared is None:
        print(f"FAIL: {DECL} does not declare `pr`; ownership is unverifiable")
        return 1
    if str(declared) != str(pr):
        print(
            f"FAIL: clearance belongs to ANOTHER PR -- {DECL} declares "
            f"pr = {declared!r}, evaluating PR {pr}"
        )
        print(
            "  A record copied or renamed from another PR does not transfer. "
            "Record clearances obtained for THIS PR."
        )
        return 1

    # Coerce defensively. `clearances = "oops"` instead of a `[clearances]`
    # table used to raise `ValueError: dictionary update sequence element #0
    # has length 1` straight out of `main`. It failed closed, but by traceback
    # -- and the docstring promises "everything that can be decided fails
    # CLOSED as 1", so a crash presenting as exit 1 makes a decision and an
    # accident indistinguishable. `required`/`watch` were safe only by luck:
    # `list("scout")` yields characters, which the narrowing check happens to
    # reject.
    try:
        required = list(decl.get("required") or [])
        watch = list(decl.get("watch") or [])
        clearances = dict(decl.get("clearances") or {})
        # INSIDE the guard, not after it. `required = [["logic"]]` gives
        # `TypeError: cannot use 'list' as a set element` -- the same
        # "crash presenting as exit 1" class this guard closed, one statement
        # further on. The fix stopped exactly where the reported symptom did.
        required_set = set(required)
        watch_set = set(watch)
    except (TypeError, ValueError) as exc:
        print(
            f"FAIL: {DECL} is malformed -- `required` and `watch` must be "
            f"arrays of strings and `[clearances]` a table: {exc}"
        )
        return 1

    narrowed = MANDATORY_VECTORS - required_set
    if narrowed:
        print(
            "FAIL: required vectors narrowed -- missing "
            f"{', '.join(sorted(narrowed))}. Ruling D's four vectors are not a "
            "per-PR choice."
        )
        return 1
    unwatched = MANDATORY_WATCH - watch_set
    if unwatched:
        print(f"FAIL: watch list narrowed -- missing {', '.join(sorted(unwatched))}.")
        return 1

    try:
        head_sha = _git("rev-parse", "--verify", "--quiet", f"{head}^{{commit}}")
        merge_base = _git("merge-base", base, head_sha)
    except RuntimeError as exc:
        print(f"FAIL: cannot resolve {head} against {base}: {exc}")
        return 2

    print(f"pr:    {pr}")
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
                f"  {', '.join(declared_only)}: declared in this PR's record only. "
                "Almost certainly a typo -- fix the name there."
            )
        if pinned:
            print(
                f"  {', '.join(pinned)}: pinned in MANDATORY_WATCH, so editing "
                "the record CANNOT clear this. If the path was legitimately "
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
    # Scoped to `required`, deliberately. Validating EVERY recorded key meant a
    # stray `banana = "not-a-sha"` -- a vector nobody reads -- hard-failed a
    # docs-only PR, defeating the one exemption this design protects on purpose.
    # Given the docstring's own warning about training people to ignore a red
    # signal, an unrelated typo should not be able to manufacture one.
    shape_errors: list[str] = []
    for vector, sha in sorted(
        (v, s) for v, s in clearances.items() if v in required_set
    ):
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
        # No shape or existence check here. `_validate_clearance_shapes` above
        # has already rejected every malformed or unresolvable value, so
        # duplicating those branches made them UNREACHABLE -- and a reviewer
        # proved it by replacing both with sentinels and watching all 20 tests
        # pass. Worse, the two tests that had covered them were loosened to
        # `in out.lower()` during that change, which is exactly what let them
        # keep passing against the new block while their names and docstrings
        # still described this loop. Two gates raising the same exit code and
        # differing only in message, with an assertion that no longer
        # discriminates between them.
        #
        # Deleted rather than re-guarded: the shape block subsumes them, and
        # dead code with a green suite is a trap for whoever edits it next.
        # Wrapped for the SAME reason the ancestry check below is: `_git` can
        # raise `GitTimeout`, and an uncaught one escapes `main` and prints a
        # traceback instead of a verdict. The ancestry call two lines down was
        # given this treatment when the timeouts landed; this one was missed.
        try:
            full = _git("rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
        except GitTimeout as exc:
            failures.append(f"{vector:16s} {exc} -- treated as NOT covered")
            continue
        try:
            anc = subprocess.run(
                ["git", "merge-base", "--is-ancestor", full, head_sha],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            failures.append(
                f"{vector:16s} ancestry check timed out after "
                f"{_GIT_TIMEOUT_SEC}s -- treated as NOT covered"
            )
            continue
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


def _cli(argv: list[str]) -> int:
    """Turn a `GitTimeout` into a verdict rather than a traceback.

    `GitTimeout` is deliberately not caught anywhere that could confuse it with
    a real answer (see `_tree`), so it has to be caught HERE -- once, at the
    boundary. Exit 2, because "git did not answer" is precisely the
    "I could not work out what to compare" case the exit codes reserve 2 for.
    Emphatically NOT 0: a check that cannot see the tree has not cleared it.
    """
    try:
        return main(argv)
    except GitTimeout as exc:
        print(f"FAIL: {exc}")
        print(
            "  git did not answer, so the comparison could not be made. This "
            "is NOT 'no delta' -- nothing was determined."
        )
        return 2
    except PRIdentityUnresolved as exc:
        print(f"FAIL: {exc}")
        print(
            "  Exit 2, NOT 1, and the distinction is the point: 1 means "
            "'this PR's clearances do not hold', which is a verdict about a "
            "known subject. Nothing was determined here -- there is no known "
            "subject. Collapsing the two would report a specific, "
            "checkable-sounding reason for a run that never identified what "
            "it was checking."
        )
        return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
