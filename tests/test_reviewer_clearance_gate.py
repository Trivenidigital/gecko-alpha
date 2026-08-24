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


def _run(repo, head="HEAD", base="master"):
    """Run the real script against `repo`, with the declaration inside it."""
    r = subprocess.run(
        [sys.executable, str(SCRIPT), head, base],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env={**os.environ, "REVIEWERS_DECL": str(repo / ".reviewers.toml")},
    )
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


def _decl(repo, clearances, required=None, watch=None):
    required = MANDATORY_VECTORS if required is None else required
    watch = MANDATORY_WATCH if watch is None else watch
    body = "required = [" + ", ".join(f'"{v}"' for v in required) + "]\n"
    body += "watch = [" + ", ".join(f'"{w}"' for w in watch) + "]\n\n[clearances]\n"
    for k, v in clearances.items():
        body += f'{k} = "{v}"\n'
    (repo / ".reviewers.toml").write_text(body)


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
    assert ".reviewers.toml" in out


# --------------------------------------------------------------------------
# The declaration actually shipped here.
# --------------------------------------------------------------------------

def test_the_shipped_declaration_names_every_mandatory_vector_and_path():
    import tomllib

    decl = tomllib.loads((REPO_ROOT / ".reviewers.toml").read_text(encoding="utf-8"))
    assert set(decl["required"]) >= set(MANDATORY_VECTORS)
    assert set(decl["watch"]) >= set(MANDATORY_WATCH)


def test_the_shipped_declaration_records_no_stale_clearances():
    """Master carries none: they are per-PR and belong on the PR branch.

    Carrying a previous PR's SHAs would inherit a misleading verdict onto every
    new branch -- and because this repo squash-merges, those SHAs stop being
    ancestors of master the moment the PR lands, so the verdict would be
    'IS NOT AN ANCESTOR' on work that has nothing to do with them.
    """
    import tomllib

    decl = tomllib.loads((REPO_ROOT / ".reviewers.toml").read_text(encoding="utf-8"))
    assert decl.get("clearances", {}) == {}, (
        "master's declaration must not carry clearance SHAs; record them on the "
        "PR branch that obtained them"
    )


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
