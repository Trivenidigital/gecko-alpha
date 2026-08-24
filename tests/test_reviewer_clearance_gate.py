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
MANDATORY_WATCH = ["scout", "scripts", "tests", ".github", "dashboard"]


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


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    for d in MANDATORY_WATCH:
        (r / d).mkdir(parents=True, exist_ok=True)
    assert subprocess.run(["git", "init", "-q", str(r)], capture_output=True).returncode == 0
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    for d in MANDATORY_WATCH:
        (r / d / "f.txt").write_text("base\n")
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


def test_a_BRANCH_NAME_is_rejected_where_a_sha_belongs(repo):
    """A moving ref silently re-points as it moves; it records nothing."""
    _decl(repo, {v: "master" for v in MANDATORY_VECTORS})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    _branch_with(repo, "prod", "scout/f.txt", "moved\n")
    rc, out = _run(repo, "HEAD", "master")
    assert rc == 1, out
    assert "NOT A FULL 40-HEX SHA" in out


def test_an_UNKNOWN_sha_names_the_right_reason(repo):
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
    assert "IS NOT A COMMIT" in out


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
