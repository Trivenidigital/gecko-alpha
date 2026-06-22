from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT_SRC = REPO_ROOT / "scripts" / "report_autonomous_status.mjs"


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "tasks").mkdir()
    (repo / "docs" / "superpowers" / "templates").mkdir(parents=True)
    shutil.copy2(SCRIPT_SRC, repo / "scripts" / "report_autonomous_status.mjs")

    (repo / "backlog.md").write_text(
        "\n".join(
            [
                "# Backlog",
                "### BL-NEW-HERMES-CODEX-OPERATING-MODEL",
                "**Status:** PROPOSED",
                "### BL-NEW-LIVE-DECISION-COCKPIT",
                "**Status:** SHIPPED-PARTIAL",
                "### BL-NEW-SIGNAL-TRUST-ROADMAP",
                "**Status:** PARTIALLY-SHIPPED",
                "",
            ]
        ),
        encoding="utf8",
    )
    (repo / "tasks" / "todo.md").write_text("# Todo\n", encoding="utf8")
    for name in [
        "README.md",
        "implementation_session.md",
        "findings_only_session.md",
        "runtime_state_verification.md",
        "vendor_probe_packet.md",
        "pr_review.md",
        "no_build_decision.md",
        "closeout_report.md",
    ]:
        (repo / "docs" / "superpowers" / "templates" / name).write_text(
            f"# {name}\n", encoding="utf8"
        )

    assert _run(["git", "init"], repo).returncode == 0
    assert _run(["git", "config", "user.email", "test@example.com"], repo).returncode == 0
    assert _run(["git", "config", "user.name", "Test User"], repo).returncode == 0
    assert _run(["git", "add", "."], repo).returncode == 0
    assert _run(["git", "commit", "-m", "init"], repo).returncode == 0
    return repo


def _run_report(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return _run(["node", "scripts/report_autonomous_status.mjs", *args], repo)


def test_reporter_is_reference_only_when_no_launcher_exists(tmp_path: Path):
    repo = _init_repo(tmp_path)

    result = _run_report(repo)

    assert result.returncode == 0, result.stderr
    assert "Runner candidates" in result.stdout
    assert (
        "No in-tree runner candidates found for `gecko-overnight-autonomous-closeout`"
        in result.stdout
    )
    assert "First-run behavior: manual/runbook-driven" in result.stdout
    assert "Reference-only mentions" in result.stdout
    assert "`scripts/report_autonomous_status.mjs`" in result.stdout
    assert "reporter-self-reference" in result.stdout


def test_cron_and_systemd_closeout_launchers_are_runner_candidates(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "cron").mkdir()
    (repo / "systemd").mkdir()
    (repo / "cron" / "gecko-alpha.crontab").write_text(
        "0 2 * * * /root/gecko-alpha/scripts/gecko-overnight-autonomous-closeout.sh\n",
        encoding="utf8",
    )
    (repo / "systemd" / "gecko-overnight-autonomous-closeout.timer").write_text(
        "[Timer]\nOnCalendar=*-*-* 02:00:00\nUnit=gecko-overnight-autonomous-closeout.service\n",
        encoding="utf8",
    )
    (repo / "systemd" / "gecko-overnight-autonomous-closeout.service").write_text(
        "[Service]\nExecStart=/root/gecko-alpha/scripts/gecko-overnight-autonomous-closeout.sh\n",
        encoding="utf8",
    )
    assert _run(["git", "add", "cron", "systemd"], repo).returncode == 0
    assert _run(["git", "commit", "-m", "add closeout runner"], repo).returncode == 0

    result = _run_report(repo)

    assert result.returncode == 0, result.stderr
    assert "Runner candidates" in result.stdout
    assert "`cron/gecko-alpha.crontab`" in result.stdout
    assert "`systemd/gecko-overnight-autonomous-closeout.timer`" in result.stdout
    assert "`systemd/gecko-overnight-autonomous-closeout.service`" in result.stdout
    assert "No in-tree runner candidates found" not in result.stdout


def test_cron_documentation_mentions_are_reference_only(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "cron").mkdir()
    (repo / "cron" / "README.md").write_text(
        "The gecko-overnight-autonomous-closeout loop is manual today.\n",
        encoding="utf8",
    )
    assert _run(["git", "add", "cron/README.md"], repo).returncode == 0
    assert _run(["git", "commit", "-m", "add cron docs"], repo).returncode == 0

    result = _run_report(repo)

    assert result.returncode == 0, result.stderr
    assert "No in-tree runner candidates found" in result.stdout
    assert "`cron/README.md` (matched: gecko-overnight-autonomous-closeout; reference-only)" in result.stdout


def test_non_markdown_cron_notes_mentions_are_reference_only(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "cron").mkdir()
    (repo / "cron" / "NOTES.txt").write_text(
        "The gecko-overnight-autonomous-closeout loop remains manual for now.\n",
        encoding="utf8",
    )
    assert _run(["git", "add", "cron/NOTES.txt"], repo).returncode == 0
    assert _run(["git", "commit", "-m", "add cron notes"], repo).returncode == 0

    result = _run_report(repo)

    assert result.returncode == 0, result.stderr
    assert "No in-tree runner candidates found" in result.stdout
    assert "`cron/NOTES.txt` (matched: gecko-overnight-autonomous-closeout; reference-only)" in result.stdout


def test_out_refuses_to_overwrite_tracked_tasks_file(tmp_path: Path):
    repo = _init_repo(tmp_path)

    result = _run_report(repo, "--out", "tasks/todo.md")

    assert result.returncode == 2
    assert "refusing to overwrite tracked file: tasks" in result.stderr


def test_out_writes_untracked_tasks_markdown(tmp_path: Path):
    repo = _init_repo(tmp_path)
    out_path = repo / "tasks" / "autonomous_status_report_new.md"

    result = _run_report(repo, "--out", "tasks/autonomous_status_report_new.md")

    assert result.returncode == 0, result.stderr
    assert out_path.exists()
    assert out_path.read_text(encoding="utf8") == result.stdout
