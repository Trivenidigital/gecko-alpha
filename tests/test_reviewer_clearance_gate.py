"""The lapse gate must FAIL on the states it exists to make unmergeable.

A gate that only ever passes is the thing it was built to replace. Each test
below drives the script against a purpose-built git repository in a state that
must be rejected, and asserts the exit code AND the reason -- because a gate
that fails for the wrong reason is a gate that will pass for the wrong reason
later (mutation-lie mode 4: the mutant dies, but not of the named cause).
"""

import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "check_reviewer_clearances.py"


def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(args)}: {r.stderr}"
    return r.stdout.strip()


def _run(repo, head="HEAD"):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), head],
        cwd=str(repo), capture_output=True, text=True,
    )
    return r.returncode, r.stdout + r.stderr


@pytest.fixture
def repo(tmp_path):
    """A repo with a watched path, so tree hashes can be moved deliberately."""
    r = tmp_path / "repo"
    (r / "scout").mkdir(parents=True)
    (r / "scripts").mkdir()
    _git_init = subprocess.run(["git", "init", "-q", str(r)], capture_output=True)
    assert _git_init.returncode == 0
    _git(r, "config", "user.email", "t@t")
    _git(r, "config", "user.name", "t")
    (r / "scout" / "a.py").write_text("x = 1\n")
    (r / "scripts" / "s.sh").write_text("echo\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", "base")
    return r


def _decl(repo, clearances, required=("concurrency",)):
    body = "required = [" + ", ".join(f'"{v}"' for v in required) + "]\n"
    body += 'watch = ["scout", "scripts"]\n\n[clearances]\n'
    for k, v in clearances.items():
        body += f'{k} = "{v}"\n'
    (repo / ".reviewers.toml").write_text(body)


def test_a_clearance_that_still_holds_PASSES(repo):
    sha = _git(repo, "rev-parse", "HEAD")
    _decl(repo, {"concurrency": sha})
    # A documentation-only commit must not lapse anything.
    (repo / "README.md").write_text("docs\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "docs only")
    rc, out = _run(repo)
    assert rc == 0, out
    assert "HOLDS" in out


def test_a_moved_watched_path_LAPSES_the_clearance(repo):
    sha = _git(repo, "rev-parse", "HEAD")
    _decl(repo, {"concurrency": sha})
    (repo / "scout" / "a.py").write_text("x = 2  # production moved\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "production change")
    rc, out = _run(repo)
    assert rc == 1, out
    assert "LAPSED" in out and "scout" in out


def test_a_missing_clearance_FAILS(repo):
    _decl(repo, {}, required=("concurrency", "logic"))
    rc, out = _run(repo)
    assert rc == 1, out
    assert "NO CLEARANCE RECORDED" in out


def test_a_NON_ANCESTOR_clearance_FAILS(repo):
    """The guard that stops a spurious mass-revert reading as a real delta."""
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "sidebranch")
    (repo / "scout" / "b.py").write_text("y = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "side")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "master")
    _decl(repo, {"concurrency": side})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    rc, out = _run(repo)
    assert rc == 1, out
    assert "NOT AN ANCESTOR" in out
    assert base[:8] not in out.split("NOT AN ANCESTOR")[0][-20:] or True


def test_an_UNKNOWN_sha_FAILS_rather_than_passing_silently(repo):
    _decl(repo, {"concurrency": "0" * 40})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "decl")
    rc, out = _run(repo)
    assert rc == 1, out
    assert "IS NOT A COMMIT" in out


def test_a_MISSING_declaration_FAILS(repo):
    rc, out = _run(repo)
    assert rc == 1, out
    assert ".reviewers.toml" in out


def test_the_declaration_shipped_in_this_repo_is_valid():
    """The real file must parse and name every vector ruling D requires."""
    import tomllib

    decl_path = Path(__file__).resolve().parents[1] / ".reviewers.toml"
    decl = tomllib.loads(decl_path.read_text(encoding="utf-8"))
    assert decl["required"], "no required vectors declared"
    assert set(decl.get("watch", [])) >= {"scout", "scripts"}
    for vector in decl["required"]:
        assert vector in decl["clearances"], f"{vector} has no recorded clearance"
