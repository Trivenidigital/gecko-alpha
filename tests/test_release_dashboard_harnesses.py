"""Disposable release harness boundaries, without touching production."""

import importlib.util
import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    path = ROOT / "tasks" / f"release_{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_manifest_rejects_core_allowlist_and_mutated_blob(tmp_path):
    mod = load("dashboard_manifest")
    with pytest.raises(ValueError):
        mod.metadata_path("scout/config.py")
    assert mod.metadata_path("tasks/release_dashboard_manifest.py")
    file = tmp_path / "sample"
    file.write_bytes(b"abc")
    expected = mod.blob_hash(file.read_bytes())
    assert expected == "f2ba8f84ab5c1bce84a7b441cb1959cfc7093b7f"
    file.write_bytes(b"abcd")
    assert mod.blob_hash(file.read_bytes()) != expected


def test_backup_preserves_source_and_refuses_destination_reuse(tmp_path):
    mod = load("dashboard_backup")
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE evidence (value)")
        db.execute("INSERT INTO evidence VALUES (42)")
    before = source.read_bytes()
    dest = tmp_path / "backup.sqlite"
    result = mod.backup(source, dest, seconds=5, minimum_free=0)
    assert result["quick_check"] == "ok"
    assert source.read_bytes() == before
    with pytest.raises(FileExistsError):
        mod.backup(source, dest, seconds=5, minimum_free=0)


def test_backup_deadline_leaves_no_valid_success(tmp_path):
    mod = load("dashboard_backup")
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE evidence (value)")
    with pytest.raises(TimeoutError):
        mod.backup(source, tmp_path / "partial.sqlite", seconds=-1, minimum_free=0)


def test_copy_guard_rejects_escape_and_attached_database(tmp_path):
    mod = load("dashboard_validate")
    copy = tmp_path / "copy.sqlite"
    with sqlite3.connect(copy) as db:
        db.execute("CREATE TABLE signal_params (signal_type,enabled)")
        db.execute("INSERT INTO signal_params VALUES ('x',1)")
    real = sqlite3.connect
    guarded = mod.guarded_connect(real, tmp_path, [])
    with pytest.raises(PermissionError):
        guarded(tmp_path.parent / "outside.sqlite")
    with guarded(copy) as db:
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("ATTACH DATABASE ':memory:' AS other")
    first = mod.fingerprint(copy)
    with real(copy) as db:
        db.execute("UPDATE signal_params SET enabled=0")
    second = mod.fingerprint(copy)
    assert first["policy"] != second["policy"]
    assert first["schema"] == second["schema"]


def test_manifest_rejects_core_delta_in_real_git_tree(tmp_path):
    mod = load("dashboard_manifest")

    def git(*args):
        return (
            subprocess.check_output(["git", "-C", str(tmp_path), *args])
            .decode()
            .strip()
        )

    git("init", "-q")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard/db.py").write_text(
        "\n".join(f"def {name}(): return 1" for name in mod.CONSUMERS)
    )
    (tmp_path / "core.py").write_text("BASE = 1\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    prs = {}
    for number in ("575", "577", "578", "580", "583"):
        (tmp_path / f"dashboard/view{number}.py").write_text("VALUE = 1\n")
        git("add", ".")
        git("commit", "-qm", number)
        prs[number] = git("rev-parse", "HEAD")
    final = prs["583"]
    with pytest.raises(ValueError, match="all five selected merged PRs"):
        mod.build_manifest(
            tmp_path, base, final, {k: v for k, v in prs.items() if k != "583"}
        )
    assert mod.build_manifest(tmp_path, base, final, prs, final)["verified"]
    (tmp_path / "core.py").write_text("BASE = 2\n")
    git("add", ".")
    git("commit", "-qm", "unexpected core change")
    with pytest.raises(ValueError, match="selective tree mismatch: core.py"):
        mod.build_manifest(tmp_path, base, final, prs, git("rev-parse", "HEAD"))


def test_trigger_targets_and_duplicate_rows_are_compared(tmp_path):
    mod = load("dashboard_validate")
    path = tmp_path / "copy.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(
            "CREATE TABLE signal_params (enabled); CREATE TABLE bookkeeping (value); CREATE TRIGGER record AFTER INSERT ON signal_params BEGIN INSERT INTO bookkeeping VALUES (1); END;"
        )
    before = mod.fingerprint(path, ["bookkeeping"])
    writes = []
    with mod.guarded_connect(sqlite3.connect, tmp_path, writes)(path) as db:
        db.execute("INSERT INTO signal_params VALUES (1)")
        db.execute("INSERT INTO signal_params VALUES (1)")
    assert "bookkeeping" in {w["table"] for w in writes}
    after = mod.fingerprint(path, ["bookkeeping"])
    assert after["policy"]["bookkeeping"]["count"] == 2
    assert before["policy"]["bookkeeping"] != after["policy"]["bookkeeping"]
    readonly = [True]
    with mod.guarded_connect(sqlite3.connect, tmp_path, [], readonly)(path) as db:
        with pytest.raises(sqlite3.DatabaseError):
            db.execute("DELETE FROM signal_params")


def test_source_attestation_rejects_untracked_import_shadow(tmp_path):
    mod = load("dashboard_validate")
    manifest = {"expected_files": {}, "metadata_allowlist": []}
    (tmp_path / "sqlite3.py").write_text("pass")
    with pytest.raises(ValueError, match="unexpected source file"):
        mod.attest(tmp_path, manifest)


def test_fingerprint_bounds_and_order_independent_multiplicity(tmp_path, monkeypatch):
    mod = load("dashboard_validate")
    assert mod.row_fingerprint(iter([(1,), (2,), (1,)])) == mod.row_fingerprint(
        iter([(2,), (1,), (1,)])
    )
    assert mod.row_fingerprint(iter([(1,), (1,)])) != mod.row_fingerprint(iter([(1,)]))
    monkeypatch.setattr(mod, "MAX_FINGERPRINT_ROWS", 2)
    with pytest.raises(ValueError, match="row budget"):
        mod.row_fingerprint((i,) for i in range(3))
    path = tmp_path / "large.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE signal_params (value)")
        db.execute(
            "INSERT INTO signal_params VALUES (?)",
            (b"x" * (mod.MAX_SQLITE_ROW_BYTES + 1),),
        )
    with pytest.raises(sqlite3.DataError):
        mod.fingerprint(path)


@pytest.fixture
def successor_git(tmp_path, monkeypatch):
    mod = load("dashboard_manifest")

    def git(*args):
        return subprocess.check_output(
            ["git", "-C", str(tmp_path), *args], stderr=subprocess.PIPE
        ).decode().strip()

    def commit(path, content, message):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        git("add", ".")
        git("commit", "-qm", message)
        return git("rev-parse", "HEAD")

    git("init", "-q")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.invalid")
    (tmp_path / "dashboard").mkdir()
    (tmp_path / "dashboard/db.py").write_text(
        "\n".join(f"def {name}(): return 1" for name in mod.CONSUMERS)
    )
    ui_paths = (
        "dashboard/frontend/components/TodayFocusPanel.jsx",
        "dashboard/frontend/components/TradeInboxTab.jsx",
    )
    # Tiny independent Git blobs keep this guard fixture usable in shallow CI.
    # Actual release pins are separately checked by the real manifest proof.
    original_ui, replacement_ui = b"ORIGINAL = 1\n", b"LANE = 1\n"
    original_blob = subprocess.check_output(
        ["git", "hash-object", "--stdin"], input=original_ui
    ).decode().strip()
    replacement_blob = subprocess.check_output(
        ["git", "hash-object", "--stdin"], input=replacement_ui
    ).decode().strip()
    monkeypatch.setattr(mod, "CORE_UI_EXCEPTIONS", {
        path: {
            "original": {"mode": "100644", "blob": original_blob},
            "replacement": {"mode": "100644", "blob": replacement_blob},
        } for path in ui_paths
    })
    for path in ui_paths:
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(original_ui)
    base = commit("dashboard/protected.py", "CORE = 1\n", "core")
    prs = {}
    for number in ("575", "577", "578", "580", "583"):
        prs[number] = commit(f"dashboard/view{number}.py", "VALUE = 1\n", number)
    old_master = prs["583"]
    git("checkout", "-qb", "rollback")
    runtime = commit("tasks/release_proof.md", "Frozen proof\n", "release")
    prior = json.loads(json.dumps(mod.build_manifest(
        tmp_path, base, old_master, prs, runtime, ["tasks/release_proof.md"]
    )))
    git("checkout", "-qb", "features", old_master)
    for number in ("584", "585"):
        if number == "584":
            for path in ui_paths:
                (tmp_path / path).write_bytes(replacement_ui)
        prs[number] = commit(f"dashboard/view{number}.py", "VALUE = 1\n", number)
    master = prs["585"]
    git("checkout", "-qb", "candidate", runtime)
    for number in ("584", "585"):
        git("checkout", master, "--", f"dashboard/view{number}.py")
    git("checkout", master, "--", *ui_paths)
    candidate = commit("tasks/release_successor.md", "New proof\n", "successor")
    kwargs = dict(
        repo=tmp_path, base=base, master=master, prs=prs, candidate=candidate,
        metadata=["tasks/release_proof.md", "tasks/release_successor.md"],
        runtime_base=runtime, rollback_branch="rollback", runtime_manifest=prior,
    )
    return mod, kwargs, git, commit


def test_successor_preserves_core_and_distinct_runtime_tree(successor_git):
    mod, kwargs, _, _ = successor_git
    build = getattr(mod, "build_successor_manifest", None)
    assert callable(build), "successor manifest boundary is missing"
    result = build(**kwargs)
    assert result["verified"]
    assert result["production_base_sha"] == kwargs["base"]
    assert result["runtime_base_sha"] == kwargs["runtime_base"]
    assert result["rollback_branch"] == "rollback"
    assert "tasks/release_proof.md" in result["runtime_baseline_files"]
    assert "tasks/release_proof.md" not in result["baseline_files"]
    assert result["core_files"]["dashboard/protected.py"] == kwargs["runtime_manifest"]["core_files"]["dashboard/protected.py"]
    assert len(result["original_core_files"]) == len(result["core_files"]) + 2
    assert set(result["core_ui_exceptions"]) == {
        "dashboard/frontend/components/TodayFocusPanel.jsx",
        "dashboard/frontend/components/TradeInboxTab.jsx",
    }
    assert result["candidate_metadata_files"]["tasks/release_successor.md"] == {
        "mode": "100644", "blob": subprocess.check_output(
            ["git", "-C", str(kwargs["repo"]), "rev-parse", f"{kwargs['candidate']}:tasks/release_successor.md"]
        ).decode().strip(),
    }


@pytest.mark.parametrize("fault", ["core_repointed", "runtime_repointed", "rollback_drift", "prior_unverified", "prior_core_missing", "missing_pr", "extra_pr", "intermediate_pr", "candidate_not_descendant", "metadata_widened"])
def test_successor_rejects_identity_and_protection_drift(successor_git, fault):
    mod, kwargs, git, _ = successor_git
    build = getattr(mod, "build_successor_manifest", None)
    assert callable(build), "successor manifest boundary is missing"
    if fault == "core_repointed":
        kwargs["base"] = kwargs["runtime_base"]
    elif fault == "runtime_repointed":
        kwargs["runtime_base"] = kwargs["master"]
    elif fault == "rollback_drift":
        git("branch", "-f", "rollback", kwargs["master"])
    elif fault == "prior_unverified":
        kwargs["runtime_manifest"]["verified"] = False
    elif fault == "prior_core_missing":
        kwargs["runtime_manifest"]["core_files"].pop("dashboard/protected.py")
    elif fault == "missing_pr":
        kwargs["prs"].pop("585")
    elif fault == "extra_pr":
        kwargs["prs"]["999"] = kwargs["master"]
    elif fault == "intermediate_pr":
        kwargs["prs"]["585"] = kwargs["candidate"]
    elif fault == "candidate_not_descendant":
        kwargs["candidate"] = kwargs["master"]
    elif fault == "metadata_widened":
        kwargs["metadata"].append("tasks/health_successor_unreviewed.py")
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        build(**kwargs)


def test_newly_selected_path_cannot_escape_original_core(successor_git):
    mod, kwargs, git, commit = successor_git
    build = getattr(mod, "build_successor_manifest", None)
    assert callable(build), "successor manifest boundary is missing"
    git("checkout", "features")
    changed = commit("dashboard/protected.py", "CORE = 2\n", "585 replacement")
    kwargs["prs"]["585"] = changed
    kwargs["master"] = changed
    kwargs["candidate"] = None
    with pytest.raises(ValueError, match="protected core"):
        build(**kwargs)


def test_successor_metadata_is_exact():
    mod = load("dashboard_manifest")
    for kind in ("plan", "design", "report"):
        path = f"tasks/{kind}_dashboard_health_successor_release_2026_09_14.md"
        assert mod.metadata_path(path) == path
        with pytest.raises(ValueError):
            mod.metadata_path(path.replace("2026_09_14", "2026_09_15"))


@pytest.mark.parametrize("fault", ["final_blob", "final_mode", "original_blob", "third_metadata_exemption"])
def test_ui_exception_pins_cannot_be_widened(successor_git, fault):
    mod, kwargs, git, commit = successor_git
    build = getattr(mod, "build_successor_manifest", None)
    assert callable(build), "successor manifest boundary is missing"
    path = "dashboard/frontend/components/TodayFocusPanel.jsx"
    if fault == "original_blob":
        kwargs["runtime_manifest"]["core_files"][path]["blob"] = "0" * 40
    elif fault == "third_metadata_exemption":
        kwargs["metadata"].append("dashboard/protected.py")
    else:
        git("checkout", "features")
        if fault == "final_blob":
            changed = commit(path, "UNREVIEWED = true\n", "unreviewed UI")
        else:
            git("update-index", "--chmod=+x", path)
            git("commit", "-qm", "unexpected mode")
            changed = git("rev-parse", "HEAD")
        kwargs["prs"]["585"] = changed
        kwargs["master"] = changed
        kwargs["candidate"] = None
    with pytest.raises(ValueError):
        build(**kwargs)
