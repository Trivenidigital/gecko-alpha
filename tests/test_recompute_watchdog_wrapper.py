"""The wrapper's exit contract.

A dead watchdog and a firing watchdog both exited 1: `cd "$APP_DIR"` fails
under `set -euo pipefail` with status 1, which is the same code the alarm path
uses after a successful Telegram send. Nothing downstream could tell them
apart, and three of five outcomes notified nobody.

Reachable straight from the runbook's install step -- the script goes to
/usr/local/bin while APP_DIR defaults to /root/gecko-alpha, and that exact
split has already caused a deploy that shipped nothing on this box.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

WRAPPER = (
    Path(__file__).resolve().parents[1] / "scripts" / "recompute-coverage-watchdog.sh"
)

# Matches the convention the other wrapper tests use. On win32 the `bash` on
# PATH is WSL's, which cannot execute a Windows-path script; CI on Linux is
# authoritative for these.
pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash wrapper semantics are Linux-specific",
)


def _run(tmp_path, *, app_dir=None, python=None, checker_exit=0, env_file=True):
    app = Path(app_dir) if app_dir else tmp_path / "app"
    if app_dir is None:
        (app / "scripts").mkdir(parents=True, exist_ok=True)
        (app / "scripts" / "check_recompute_coverage.py").write_text(
            "", encoding="utf-8"
        )
        if env_file:
            (app / ".env").write_text(
                "TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n", encoding="utf-8"
            )

    if python is None:
        py = tmp_path / "fakepython"
        py.write_text(
            f"#!/usr/bin/env bash\necho stub\nexit {checker_exit}\n", encoding="utf-8"
        )
        py.chmod(0o755)
        python = str(py)

    env = dict(os.environ)
    env.update(
        GECKO_APP_DIR=str(app),
        GECKO_DB_PATH=str(tmp_path / "scout.db"),
        GECKO_ENV_FILE=str(app / ".env"),
        GECKO_PYTHON=python,
    )
    return subprocess.run(
        ["bash", str(WRAPPER)], capture_output=True, text=True, env=env
    )


def test_a_missing_app_dir_is_not_confused_with_an_alarm(tmp_path):
    r = _run(tmp_path, app_dir=str(tmp_path / "nope"))
    assert r.returncode == 5, (r.returncode, r.stdout, r.stderr)
    assert "APP_DIR does not exist" in r.stderr


def test_a_missing_interpreter_is_not_confused_with_an_alarm(tmp_path):
    r = _run(tmp_path, python=str(tmp_path / "no-such-python"))
    assert r.returncode == 6, (r.returncode, r.stdout, r.stderr)
    assert "python interpreter not executable" in r.stderr


def test_a_healthy_check_exits_zero(tmp_path):
    r = _run(tmp_path, checker_exit=0)
    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "OK:" in r.stdout


def test_a_check_that_could_not_run_is_distinct_from_healthy(tmp_path):
    """exit 2 must not read as an all-clear."""
    r = _run(tmp_path, checker_exit=2)
    assert r.returncode == 2, (r.returncode, r.stdout, r.stderr)


def test_missing_telegram_credentials_do_not_silently_swallow_the_alarm(tmp_path):
    """The alarm fired; delivery could not happen. That is its own code."""
    r = _run(tmp_path, checker_exit=1)
    assert r.returncode == 4, (r.returncode, r.stdout, r.stderr)
    assert "ALERT:" in r.stdout


def test_a_missing_env_file_is_its_own_code(tmp_path):
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True, exist_ok=True)
    r = _run(tmp_path, checker_exit=1, env_file=False)
    assert r.returncode == 3, (r.returncode, r.stdout, r.stderr)


def test_every_documented_exit_code_is_distinct():
    """The contract is only useful if the codes do not collide."""
    text = WRAPPER.read_text(encoding="utf-8")
    codes = {
        int(line.split("exit ")[1].split()[0])
        for line in text.splitlines()
        if line.strip().startswith("exit ") and line.strip()[5:].strip().isdigit()
    }
    assert len(codes) == len([c for c in codes]), "duplicate exit codes"
    assert {0, 1, 2, 3, 4, 5, 6} >= codes, f"undocumented exit code in {codes}"
