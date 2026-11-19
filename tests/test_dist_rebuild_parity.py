"""Real Git fixtures distinguish fresh-build parity from asset existence."""

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/check_dist_rebuild_parity.py"


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args]).decode().strip()


@pytest.fixture
def sample(tmp_path):
    repo = tmp_path / "repo"
    dist = repo / "dashboard/frontend/dist"
    dist.mkdir(parents=True)
    (dist / "assets").mkdir()
    (dist / "index.html").write_text('<script src="/assets/main.js"></script>')
    (dist / "assets/main.js").write_bytes(b"console.log('current');\n")
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Parity test")
    git(repo, "config", "core.autocrlf", "false")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "fixture")
    build = tmp_path / "fresh"
    shutil.copytree(dist, build)
    return repo, dist, build


@pytest.fixture
def checker():
    assert SCRIPT.is_file(), "fresh-build comparator is missing"
    spec = importlib.util.spec_from_file_location("parity", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_matching_build_identifies_commit_and_keeps_historical_assets(sample, checker):
    repo, dist, build = sample
    (dist / "assets/old.js").write_bytes(b"historical")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "historical")
    result = checker.compare_build(repo, build)
    assert result["head_sha"] == git(repo, "rev-parse", "HEAD")
    assert result["file_count"] == 2
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("path", ["assets/main.js", "index.html"])
def test_same_names_different_bytes_fail(sample, checker, path):
    repo, _, build = sample
    with (build / path).open("ab") as out:
        out.write(b" changed")
    with pytest.raises(checker.ParityError, match="byte_mismatch"):
        checker.compare_build(repo, build)


def test_untracked_artifact_cannot_satisfy_missing_git_blob(sample, checker):
    repo, dist, build = sample
    for root in (dist, build):
        (root / "lazy.js").write_bytes(b"new chunk")
    with pytest.raises(checker.ParityError, match="missing_committed_file"):
        checker.compare_build(repo, build)


def test_lazy_chunk_is_checked_even_without_html_reference(sample, checker):
    repo, dist, build = sample
    (dist / "lazy.js").write_bytes(b"old chunk")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "lazy chunk")
    (build / "lazy.js").write_bytes(b"changed chunk")
    with pytest.raises(checker.ParityError, match="byte_mismatch"):
        checker.compare_build(repo, build)


@pytest.mark.parametrize(
    "ref",
    [
        "../main.js",
        "https://host/a.js",
        "%2e%2e/a.js",
        "assets/missing.js",
        "",
        "assets%5cmain.js",
    ],
)
def test_invalid_or_missing_references_fail(sample, checker, ref):
    repo, _, build = sample
    (build / "index.html").write_text(f'<script src="{ref}"></script>')
    with pytest.raises(checker.ParityError, match="reference"):
        checker.compare_build(repo, build)


def test_missing_index_fails(sample, checker):
    repo, _, build = sample
    (build / "index.html").unlink()
    with pytest.raises(checker.ParityError, match="index"):
        checker.compare_build(repo, build)


def test_output_inside_checkout_fails(sample, checker):
    repo, dist, _ = sample
    with pytest.raises(checker.ParityError, match="build_directory"):
        checker.compare_build(repo, dist)


def test_dirty_source_fails(sample, checker):
    repo, dist, build = sample
    (dist / "assets/main.js").write_bytes(b"dirty")
    with pytest.raises(checker.ParityError, match="git_failure"):
        checker.compare_build(repo, build)


def test_symlink_output_fails(sample, checker):
    repo, dist, build = sample
    try:
        (build / "alias.js").symlink_to(dist / "assets/main.js")
    except OSError:
        pytest.skip("OS denies symlink creation")
    with pytest.raises(checker.ParityError, match="unsafe_build_entry"):
        checker.compare_build(repo, build)


def test_head_changed_during_read_fails(sample, checker, monkeypatch):
    repo, _, build = sample
    original = checker._git
    changed = False

    def changing(root, *args):
        nonlocal changed
        value = original(root, *args)
        if args[0] == "cat-file" and not changed:
            git(repo, "commit", "--allow-empty", "-qm", "concurrent change")
            changed = True
        return value

    monkeypatch.setattr(checker, "_git", changing)
    with pytest.raises(checker.ParityError, match="head_changed"):
        checker.compare_build(repo, build)


def test_ci_runs_fresh_build_then_comparator_without_failure_suppression():
    import yaml

    workflow = yaml.safe_load(
        (SCRIPT.parents[1] / ".github/workflows/test.yml").read_text()
    )
    job = workflow["jobs"].get("frontend-dist-parity")
    assert job is not None, "CI has no fresh-build parity job"
    assert not job.get("if") and not job.get("continue-on-error")
    steps = job["steps"]
    assert all(not s.get("continue-on-error") and not s.get("if") for s in steps)
    checkout = next(
        s for s in steps if s.get("uses", "").startswith("actions/checkout@")
    )
    assert "ref" not in checkout.get("with", {})
    node = next(s for s in steps if s.get("uses", "").startswith("actions/setup-node@"))
    assert node["with"]["node-version"] == "24.14.0"
    commands = "\n".join(s.get("run", "") for s in steps)
    assert (
        commands.index("npm ci")
        < commands.index("run build")
        < commands.index("check_dist_rebuild_parity.py")
    )
    assert "mktemp -d" in commands and "--outDir" in commands
    assert "set -euo pipefail" in commands and "|| true" not in commands
    assert "reviewer-clearances" in workflow["jobs"] and "test" in workflow["jobs"]


def test_git_failure_is_not_success(sample, checker, monkeypatch):
    repo, _, build = sample

    def unavailable(*args, **kwargs):
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(checker.subprocess, "run", unavailable)
    with pytest.raises(checker.ParityError, match="git_failure"):
        checker.compare_build(repo, build)
