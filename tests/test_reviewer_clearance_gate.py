"""The lapse detector must FAIL on the states it exists to catch.

A check that only ever passes is the thing it was built to replace. Each test
drives the script against a purpose-built repository in a state that must be
rejected, and asserts the exit code AND the reason -- because a check that
fails for the wrong reason is one that will pass for the wrong reason later.

Several of these exist because an independent reviewer broke the first draft:
a misspelled watch entry silently reported coverage, `required` could be
narrowed to one vector, a branch name was accepted where a SHA belongs, and
every PR was forced to edit the declaration to go green. The bypasses that
remain -- no branch protection, and an author-writable record -- are documented
in the script's docstring rather than papered over, because they cannot be
closed from inside the repository.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "check_reviewer_clearances.py"

MANDATORY_VECTORS = ["concurrency", "silent-failure", "ops-safety", "logic"]
MANDATORY_WATCH = [
    "scout", "scripts", "tests", ".github", "dashboard", "cron", "ops",
    "systemd", "pyproject.toml", "uv.lock", "Dockerfile", "docker-compose.yml",
    "start.sh",
]


def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


#: The PR number the single-PR tests operate on. A name rather than a magic
#: literal, so the cross-PR tests are obviously about a DIFFERENT number.
PR = "42"
OTHER_PR = "43"


def _run(repo, head="HEAD", base="master", pr=PR, env_pr=None):
    """Run the real script against `repo`, with its per-PR records inside it.

    `pr=None` omits `--pr` entirely -- that is how identity-unresolved is
    exercised. `env_pr` sets `REVIEWERS_PR`, standing in for CI supplying
    trusted event metadata; it is separate from `pr` so a test can prove both
    paths resolve the SAME record.
    """
    argv = [sys.executable, str(SCRIPT), head, base]
    if pr is not None:
        argv += ["--pr", pr]
    env = {**os.environ, "REVIEWERS_DIR": str(repo / ".reviewers")}
    env.pop("REVIEWERS_PR", None)
    if env_pr is not None:
        env["REVIEWERS_PR"] = env_pr
    r = subprocess.run(argv, cwd=str(repo), capture_output=True, text=True, env=env)
    return r.returncode, r.stdout + r.stderr


#: Watch entries that are FILES rather than directories. The fixture has to
#: know the difference: an earlier version created a directory for every entry,
#: so writing to `repo/Dockerfile` raised PermissionError against a directory.
WATCHED_FILES = {
    "pyproject.toml",
    "uv.lock",
    "Dockerfile",
    "docker-compose.yml",
    "start.sh",
}
WATCHED_DIRS = [w for w in MANDATORY_WATCH if w not in WATCHED_FILES]


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir(parents=True, exist_ok=True)
    for d in WATCHED_DIRS:
        (r / d).mkdir(parents=True, exist_ok=True)
    assert subprocess.run(["git", "init", "-q", str(r)], capture_output=True).returncode == 0
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    for d in WATCHED_DIRS:
        (r / d / "f.txt").write_text("base\n")
    for f in WATCHED_FILES:
        (r / f).write_text("base\n")
    (r / "docs").mkdir(exist_ok=True)
    (r / "docs" / "d.md").write_text("docs\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    _git(r, "branch", "-M", "master")
    return r


def _decl(repo, clearances, required=None, watch=None, pr=PR, declared_pr=None):
    """Write `.reviewers/<pr>.toml`.

    `declared_pr` overrides the `pr` field INSIDE the file while leaving the
    filename alone -- that is how "a record copied from another PR" is built,
    and it is exactly the case a filename-only convention cannot reject.
    """
    required = MANDATORY_VECTORS if required is None else required
    watch = MANDATORY_WATCH if watch is None else watch
    owner = declared_pr if declared_pr is not None else pr
    nl = chr(10)
    body = f"pr = {owner}" + nl
    body += "required = [" + ", ".join(f'"{v}"' for v in required) + "]" + nl
    body += "watch = [" + ", ".join(f'"{w}"' for w in watch) + "]" + nl + nl
    body += "[clearances]" + nl
    for k, v in clearances.items():
        body += f'{k} = "{v}"' + nl
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pr}.toml").write_text(body)


def _branch_with(repo, name, path, content):
    _git(repo, "checkout", "-q", "-b", name)
    (repo / path).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"change {path}")
    return _git(repo, "rev-parse", "HEAD")


# --------------------------------------------------------------------------
# The delta question: clearances are demanded only when production moved.
# --------------------------------------------------------------------------

def test_a_docs_only_branch_needs_NO_clearance(repo):
    """Otherwise every PR is forced to edit the record, which is the bad habit."""
    _decl(repo, {})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    _branch_with(repo, "docsonly", "docs/d.md", "more docs\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 0, out
    assert "no clearance required" in out


def test_a_branch_that_moves_a_watched_path_DEMANDS_a_clearance(repo):
    _decl(repo, {})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    _branch_with(repo, "prod", "scout/f.txt", "production moved\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "NO CLEARANCE RECORDED" in out
    assert "delta in watched paths" in out and "scout" in out


def test_a_clearance_covering_the_delta_PASSES(repo):
    _decl(repo, {})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    sha = _branch_with(repo, "prod", "scout/f.txt", "production moved\n")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "record clearances")
    # The clearance predates a docs-only commit, so it still covers the tree.
    (repo / "docs" / "d.md").write_text("docs after review\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs after review")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 0, out
    assert out.count("HOLDS") == len(MANDATORY_VECTORS)


def test_a_clearance_stops_covering_once_production_moves_again(repo):
    _decl(repo, {})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    sha = _branch_with(repo, "prod", "scout/f.txt", "first change\n")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "record")
    (repo / "scout" / "f.txt").write_text("SECOND change, unreviewed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unreviewed change")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "LAPSED" in out and "scout" in out


# --------------------------------------------------------------------------
# Bypasses an independent reviewer found in the first draft.
# --------------------------------------------------------------------------

def test_a_watch_entry_that_resolves_NOWHERE_is_fatal_not_silent(repo):
    """The only fail-OPEN path in the first draft.

    `_tree` returns None for a missing path and `None != None` is False, so a
    misspelled entry compared equal, held, and printed the typo in the banner
    as though it were covered.
    """
    _decl(repo, {}, watch=MANDATORY_WATCH + ["dashbaord"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    _branch_with(repo, "prod", "scout/f.txt", "moved\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "dashbaord" in out
    assert "monitors nothing" in out


def test_required_vectors_cannot_be_NARROWED(repo):
    _decl(repo, {}, required=["logic"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "required vectors narrowed" in out
    for v in ("concurrency", "ops-safety", "silent-failure"):
        assert v in out


def test_the_watch_list_cannot_be_NARROWED(repo):
    _decl(repo, {}, watch=["scout"])
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "watch list narrowed" in out


def test_a_BRANCH_NAME_is_rejected_by_the_SHAPE_CHECK(repo):
    """A moving ref silently re-points as it moves; it records nothing.

    Names the gate. There were briefly TWO gates rejecting this -- an early
    shape pass and a per-vector branch -- raising the same exit code and
    differing only in message. When the shape pass was added it made the loop
    branch unreachable, and this assertion had been loosened to
    `in out.lower()`, which let it keep passing against the new gate while its
    name still described the old one. A reviewer proved the branch was dead by
    replacing it with a sentinel and watching all 20 tests pass. The dead
    branch is gone; this test now pins WHICH gate raises.
    """
    _decl(repo, {v: "master" for v in MANDATORY_VECTORS})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    _branch_with(repo, "prod", "scout/f.txt", "moved\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "not a full 40-hex sha" in out.lower()
    assert "recorded clearances are malformed" in out, (
        "expected the early shape check to raise, not the per-vector loop"
    )


def test_an_UNKNOWN_sha_is_rejected_by_the_SHAPE_CHECK(repo):
    """A bare `git rev-parse` echoes any 40-hex string back unverified.

    Before `--verify ...^{commit}` this fell through to the ancestry check and
    was reported as "not an ancestor" -- true of a commit that is not in the
    repository at all, and misleading to debug.
    """
    _decl(repo, {v: "0" * 40 for v in MANDATORY_VECTORS})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    _branch_with(repo, "prod", "scout/f.txt", "moved\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "is not a commit" in out.lower()
    assert "recorded clearances are malformed" in out, (
        "expected the early shape check to raise, not the per-vector loop"
    )


def test_a_NON_ANCESTOR_clearance_FAILS(repo):
    _decl(repo, {})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    side = _branch_with(repo, "sidebranch", "scout/f.txt", "side\n")
    _git(repo, "checkout", "-q", "master")
    _branch_with(repo, "prod", "scout/f.txt", "prod\n")
    _decl(repo, {v: side for v in MANDATORY_VECTORS})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "record side sha")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "IS NOT AN ANCESTOR" in out


def test_a_MISSING_declaration_FAILS(repo):
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert PR + ".toml" in out, out
    assert "no clearance recorded for PR " + PR in out, out


# --------------------------------------------------------------------------
# PER-PR ISOLATION. These replace the master-global declaration block, which
# is GONE -- and its absence is the fix, not a coverage loss.
#
# The old design kept one `.reviewers.toml` at the repo root. Because this repo
# squash-merges, a cleared PR's SHAs landed on master, stopped being ancestors
# of anything branched from it, and required a post-merge "reset master's
# [clearances]" commit. The guard that detected a forgotten reset read master
# AS COMMITTED, so once master was stale EVERY branch went red -- including the
# branch that fixed it. Breaking that deadlock took an authorized direct push.
#
# The tests that pinned `_master_declaration_text`, the stale-clearance
# tripwire and its bootstrap-skip are deleted WITH the state they guarded.
# Reintroducing a master-global table must FAIL this suite; see
# `test_reintroducing_a_MASTER_GLOBAL_table_breaks_isolation`.
# --------------------------------------------------------------------------


def test_every_shipped_record_declares_the_pr_its_FILENAME_claims():
    """Hygiene over the records at rest -- copy/rename mistakes caught early.

    Deliberately NOT clearance evaluation: this reads every record, but the
    GATE reads exactly one. Historical records stay inert as active state while
    still being checked for the one defect that is always a bug -- a record
    whose `pr` field disagrees with its filename, which is exactly what a
    record copied from another PR looks like.
    """
    import tomllib

    d = REPO_ROOT / ".reviewers"
    if not d.is_dir():
        pytest.skip("no per-PR records shipped yet")
    for f in sorted(d.glob("*.toml")):
        decl = tomllib.loads(f.read_text(encoding="utf-8"))
        assert "pr" in decl, f"{f.name} does not declare `pr`"
        assert str(decl["pr"]) == f.stem, (
            f"{f.name} declares pr={decl['pr']!r} -- filename and contents "
            "disagree, which is what a copied record looks like"
        )
        assert set(decl.get("required", [])) >= set(MANDATORY_VECTORS), (
            f"{f.name} narrows `required` below the mandatory four"
        )


def test_ANOTHER_PRs_clearance_cannot_satisfy_this_one(repo):
    """PR A's record must never clear PR B. The filename is not the owner.

    Built the way it actually happens: the file is NAMED for this PR -- so a
    filename-only convention would accept it -- while its contents still
    declare the PR it was copied from.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR, declared_pr=OTHER_PR)
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    assert "belongs to ANOTHER PR" in out, out
    assert OTHER_PR in out and PR in out, out


def test_a_record_for_a_DIFFERENT_pr_is_not_consulted_at_all(repo):
    """Only the record matching the evaluated PR is read.

    The other PR's record is fully valid and covers the delta -- it simply is
    not this PR's. Historical records on master are inert by this same
    mechanism: nothing ever reads them.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=OTHER_PR)
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    assert "no clearance recorded for PR " + PR in out, out


def test_two_concurrent_PRs_at_different_heads_stay_INDEPENDENT(repo):
    """One PR's lapse must not lapse the other.

    Records are (re)written immediately before each evaluation rather than once
    up front. `_branch_with` runs `git add -A`, so a record written before a
    branch switch gets committed onto whichever branch happened to be checked
    out and then VANISHES on the next checkout -- which looked exactly like a
    cross-PR interaction and would have made this test pass or fail for a
    reason that has nothing to do with isolation.
    """
    a_sha = _branch_with(repo, "pr-a", "scout/f.txt", "a")
    _git(repo, "checkout", "-q", "master")
    b_sha = _branch_with(repo, "pr-b", "dashboard/f.txt", "b")

    def both_records(a, b):
        _decl(repo, {v: a for v in MANDATORY_VECTORS}, pr=PR)
        _decl(repo, {v: b for v in MANDATORY_VECTORS}, pr=OTHER_PR)

    both_records(a_sha, b_sha)
    assert _run(repo, b_sha, "master", pr=OTHER_PR)[0] == 0
    assert _run(repo, a_sha, "master", pr=PR)[0] == 0

    # Move PR A only. Its own clearance must lapse; B's must not.
    _git(repo, "checkout", "-q", "pr-a")
    (repo / "scout" / "f.txt").write_text("a2")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "a moves again")
    a2 = _git(repo, "rev-parse", "HEAD")

    both_records(a_sha, b_sha)
    rc_a2, out_a2 = _run(repo, a2, "master", pr=PR)
    assert rc_a2 == 1 and "LAPSED" in out_a2, out_a2

    rc_b2, out_b2 = _run(repo, b_sha, "master", pr=OTHER_PR)
    assert rc_b2 == 0, "PR A's lapse bled into PR B:" + chr(10) + out_b2



def test_DELETING_the_active_record_is_RED_not_a_skip(repo):
    """The failure mode that made the old guard dangerous, closed by construction.

    A red that can be cleared by DELETING the declaration is worse than no
    check: it turns the gate off while reporting success.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    assert _run(repo, "HEAD", "master", pr=PR)[0] == 0
    (repo / ".reviewers" / (PR + ".toml")).unlink()
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, out
    assert "This is NOT a skip" in out, out


def test_RENAMING_the_active_record_cannot_launder_it(repo):
    """Renaming to another PR's number is rejected by the `pr` field."""
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    d = repo / ".reviewers"
    (d / (PR + ".toml")).rename(d / (OTHER_PR + ".toml"))
    rc, out = _run(repo, "HEAD", "master", pr=OTHER_PR)
    assert rc == 1 and "belongs to ANOTHER PR" in out, out


def test_a_BRANCH_NAME_can_never_confer_ownership(repo):
    """The security boundary: identity comes from OUTSIDE the tree.

    A branch named for another PR, carrying that PR's valid record, must not be
    cleared by it. If ownership were inferred from `HEAD`, renaming a branch
    would be enough to claim any PR's clearances.
    """
    sha = _branch_with(repo, "feat/pr-" + OTHER_PR, "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=OTHER_PR)
    rc, out = _run(repo, "HEAD", "master", pr=None)
    assert rc == 2, out
    assert "no PR identity" in out, out
    assert "branch name" in out.lower(), out


def test_MISSING_pr_identity_is_exit_2_not_exit_1(repo):
    """'Which PR is this?' and 'this PR is not cleared' are different facts."""
    _branch_with(repo, "work", "scout/f.txt", "moved")
    rc, out = _run(repo, "HEAD", "master", pr=None)
    assert rc == 2, out


@pytest.mark.parametrize("bad", ["../41", "41/../42", "0", "abc", "-1"])
def test_a_MALFORMED_pr_identity_is_refused_before_touching_the_filesystem(repo, bad):
    """Traversal and padding must not resolve to another PR's record."""
    _branch_with(repo, "work", "scout/f.txt", "moved")
    rc, out = _run(repo, "HEAD", "master", pr=bad)
    assert rc == 2, out


def test_CI_and_LOCAL_identity_resolve_the_SAME_record(repo):
    """`REVIEWERS_PR` (CI event metadata) and `--pr` (local) must agree."""
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    rc_local, _ = _run(repo, "HEAD", "master", pr=PR)
    rc_ci, _ = _run(repo, "HEAD", "master", pr=None, env_pr=PR)
    assert rc_local == 0 and rc_ci == 0
    rc_wrong, out = _run(repo, "HEAD", "master", pr=None, env_pr=OTHER_PR)
    assert rc_wrong == 1, out


def test_SQUASH_MERGE_then_unrelated_work_needs_NO_reset_commit(repo):
    """THE REGRESSION FOR THE FAILURE THAT CREATED THIS DESIGN.

    Clear a PR, squash-merge it, branch unrelated work from the new master, and
    the gate must be green with no cleanup commit. Under the master-global
    table this was impossible: the merged PR's SHAs stopped being ancestors and
    poisoned every subsequent branch until someone reset master by hand.
    """
    sha = _branch_with(repo, "pr-a", "scout/f.txt", "cleared work")
    _decl(repo, {v: sha for v in MANDATORY_VECTORS}, pr=PR)
    assert _run(repo, "HEAD", "master", pr=PR)[0] == 0

    _git(repo, "checkout", "-q", "master")
    _git(repo, "merge", "--squash", "pr-a")
    _git(repo, "commit", "-qm", "squashed pr-a")

    later = _branch_with(repo, "pr-b", "dashboard/f.txt", "unrelated")
    _decl(repo, {v: later for v in MANDATORY_VECTORS}, pr=OTHER_PR)
    rc, out = _run(repo, "HEAD", "master", pr=OTHER_PR)
    assert rc == 0, (
        "unrelated work after a squash-merged cleared PR was not green -- the "
        "post-merge reset has come back:\n" + out
    )
    assert "IS NOT AN ANCESTOR" not in out, out


def test_reintroducing_a_MASTER_GLOBAL_table_breaks_isolation(repo):
    """The mutation the operator asked for, as a standing regression.

    A root `.reviewers.toml` carrying another PR's clearances must not satisfy
    this PR. If a future change ever reads a repo-global table again, this
    fails -- which is what makes the fix about ELIMINATING the state rather
    than arranging tests around it.
    """
    sha = _branch_with(repo, "work", "scout/f.txt", "moved")
    body = "required = []\nwatch = []\n\n[clearances]\n"
    for v in MANDATORY_VECTORS:
        body += v + ' = "' + sha + '"\n'
    (repo / ".reviewers.toml").write_text(body)
    rc, out = _run(repo, "HEAD", "master", pr=PR)
    assert rc == 1, (
        "a repo-global .reviewers.toml satisfied a per-PR evaluation -- the "
        "master-global table is back:\n" + out
    )
    assert "no clearance recorded for PR " + PR in out, out


# --------------------------------------------------------------------------
# The delta shortcut changed what an omission from `watch` MEANS.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path", ["cron", "ops", "systemd"])
def test_the_deploy_substrate_DEMANDS_a_clearance(repo, path):
    """Regression for a hole a fix opened.

    While a clearance was demanded unconditionally, a path missing from `watch`
    only failed to LAPSE one. Once "no delta in watched paths" became a pass,
    every omission became a silent full exemption -- reported as "no clearance
    required". `cron`, `ops` and `systemd` are the deploy substrate: a
    2-minute cron entry once migrated the live database into 74 `database is
    locked` errors, and systemd units run scripts from `/usr/local/bin` copies
    so `git pull` deploys nothing.
    """
    _decl(repo, {})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    _branch_with(repo, f"sub-{path}", f"{path}/f.txt", "deploy substrate moved\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert path in out
    assert "NO CLEARANCE RECORDED" in out


def test_a_malformed_clearance_is_caught_even_when_none_is_DEMANDED(repo):
    """Otherwise a typo hides through every docs-only PR.

    The per-vector loop never runs when there is no delta, so without this a
    malformed SHA sits unvalidated and then fails all four vectors at once on
    the first PR that touches production -- the least convenient moment to
    discover it.
    """
    _decl(repo, {v: "not-a-sha" for v in MANDATORY_VECTORS})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    _branch_with(repo, "docsonly", "docs/d.md", "docs change only\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "malformed" in out
    assert "not a full 40-hex SHA" in out


@pytest.mark.parametrize("path", ["pyproject.toml", "uv.lock", "Dockerfile"])
def test_a_ROOT_FILE_change_demands_a_clearance(repo, path):
    """A claim I shipped as a documented limitation, which was simply false.

    An earlier draft asserted root files could not be watched "because a path
    prefix cannot reach a root file", and a test pinned that gap in place.
    `git rev-parse <rev>:pyproject.toml` returns a BLOB hash; comparing blobs
    is the same operation as comparing trees. The limitation was an artifact of
    never running the claim -- and the test asserting it would have kept a
    dependency bump exempt from review indefinitely.
    """
    _decl(repo, {})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    # git refuses a ref ending in ".lock", so sanitise the branch name.
    safe = path.replace(".", "-")
    _git(repo, "checkout", "-q", "-b", f"root-{safe}")
    (repo / path).write_text("changed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"change {path}")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert path in out
    assert "NO CLEARANCE RECORDED" in out


# --------------------------------------------------------------------------
# A timeout is not an answer. The fail-open a fix introduced.
# --------------------------------------------------------------------------

def _load_checker():
    import importlib.util

    spec = importlib.util.spec_from_file_location("chk_mod", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_a_git_TIMEOUT_is_not_reported_as_an_absent_path(repo, monkeypatch):
    """The fail-open introduced by the subprocess-timeout hardening.

    `_git` originally converted `TimeoutExpired` into `RuntimeError` -- the same
    type it raises for "no such object". `_tree` catches `RuntimeError` and
    returns None, and None is `_moved`'s encoding for "path absent at that
    revision". So a timeout became indistinguishable from an absent path, and a
    reviewer showed the consequence: with two transient timeouts against a repo
    where `scout/` had genuinely moved, `_moved` returned `([], [])` and the
    check printed "no delta in watched paths; no clearance required", exit 0.

    A production change reported as needing no review, reached without touching
    the declaration. `GitTimeout` now has its own type and `_tree` does not
    catch it.
    """
    import subprocess as sp

    mod = _load_checker()
    real = sp.run
    seen = {"n": 0}

    def flaky(cmd, **kw):
        if len(cmd) > 2 and cmd[1] == "rev-parse" and ":scout" in cmd[-1]:
            seen["n"] += 1
            raise sp.TimeoutExpired(cmd, 60)
        return real(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", flaky)
    with pytest.raises(mod.GitTimeout):
        mod._moved(["scout"], "HEAD", "HEAD")
    assert seen["n"] >= 1, "the timeout was never triggered; test proves nothing"


def test_the_cli_turns_a_timeout_into_exit_2_not_a_traceback(repo, monkeypatch):
    """"git did not answer" is the "could not work out what to compare" case.

    Emphatically not 0: a check that cannot see the tree has not cleared it.
    """
    import subprocess as sp

    mod = _load_checker()
    real = sp.run

    def always_times_out(cmd, **kw):
        if len(cmd) > 1 and cmd[1] in ("rev-parse", "merge-base"):
            raise sp.TimeoutExpired(cmd, 60)
        return real(cmd, **kw)

    monkeypatch.setattr(mod.subprocess, "run", always_times_out)
    # DECL_DIR, not DECL: the declaration is per-PR now and is resolved from
    # the identity, so the directory is what the test can pin.
    monkeypatch.setattr(mod, "DECL_DIR", repo / ".reviewers")
    _decl(repo, {})
    rc = mod._cli(["check", "HEAD", "master", "--pr", PR])
    assert rc == 2, f"a timeout must not read as a pass or a clearance verdict (got {rc})"


def test_a_malformed_required_that_is_not_a_list_of_strings_FAILS_cleanly(repo):
    """The coercion guard stopped one statement short of the crash it closed.

    `required = [["logic"]]` raised `TypeError: cannot use 'list' as a set
    element` at the `set(required)` on the next line -- the same
    "crash presenting as exit 1" class, one statement further on.
    """
    d = repo / ".reviewers"
    d.mkdir(parents=True, exist_ok=True)
    nl = chr(10)
    (d / (PR + ".toml")).write_text(
        "pr = " + PR + nl
        + 'required = [["logic"]]' + nl
        + 'watch = ["scout"]' + nl + nl
        + "[clearances]" + nl
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "malformed" in out
    assert "Traceback" not in out
