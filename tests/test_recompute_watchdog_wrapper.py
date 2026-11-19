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
    # NEVER create a caller-supplied app_dir. An earlier edit added a mkdir
    # here so one test could supply its own .env, and it silently created the
    # directory that `test_a_missing_app_dir...` exists to find MISSING --
    # turning that test green against a wrapper that no longer detected the
    # case at all.
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


def test_it_runs_against_the_REAL_production_env_shape(tmp_path):
    """The prod .env has no CONVICTION_EARLY_LEAD_MINUTES. The value is a
    Python default.

    Sourcing the gate with `grep ... | tail -1 | cut` under `set -euo pipefail`
    meant grep exited 1 on that file, pipefail propagated it, and `set -e`
    killed the script before the check ran -- printing NOTHING and exiting 1,
    which is this wrapper's own alarm code. Dead while looking like it fired,
    in the alarm that exists because nothing on that box reads journald.
    """
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True, exist_ok=True)
    (app / "scripts" / "check_recompute_coverage.py").write_text("", encoding="utf-8")
    # Verbatim the production shape: conviction settings present, gate absent.
    (app / ".env").write_text(
        "CONVICTION_THRESHOLD=75\nPAPER_CONVICTION_LOCK_ENABLED=true\n"
        "TELEGRAM_BOT_TOKEN=\nTELEGRAM_CHAT_ID=\n",
        encoding="utf-8",
    )

    r = _run(tmp_path, app_dir=str(app), checker_exit=0)

    assert r.returncode == 0, (r.returncode, r.stdout, r.stderr)
    assert "OK:" in r.stdout, "the wrapper produced no output at all"


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
    # Collected into a LIST. The first version built a set and then compared
    # its length against a list built from that same set -- equal by
    # construction, so duplicates were deduplicated before the check that was
    # supposed to find them. A guard that cannot fail.
    codes = [
        int(line.split("exit ")[1].split()[0])
        for line in text.splitlines()
        if line.strip().startswith("exit ") and line.strip()[5:].strip().isdigit()
    ]
    assert codes, "no exit codes found; the scanner is reading nothing"
    # 1 is the alarm path and appears once; every other code should be unique.
    # Duplicates are what make a dead watchdog indistinguishable from a firing
    # one, which is the whole reason these codes exist.
    dupes = sorted({c for c in codes if codes.count(c) > 1})
    assert dupes == [], f"duplicate exit codes: {dupes}"
    assert {0, 1, 2, 3, 4, 5, 6, 7} >= set(codes), f"undocumented exit code in {codes}"


def test_it_refuses_rather_than_guessing_the_gate(tmp_path):
    """No literal fallback. A guessed threshold is worse than no check.

    The gate comes from the app — Settings first, then the field default in
    `scout/config.py`. A number written in the shell would drift from the one
    the readers use, which is the divergence the gate re-check exists to
    remove. If neither form answers, refuse.
    """
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True, exist_ok=True)
    (app / "scripts" / "check_recompute_coverage.py").write_text("", encoding="utf-8")
    (app / ".env").write_text("TELEGRAM_BOT_TOKEN=\n", encoding="utf-8")

    # An interpreter that runs, but cannot import the app.
    py = tmp_path / "brokenpython"
    py.write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")
    py.chmod(0o755)

    r = _run(tmp_path, app_dir=str(app), python=str(py))

    assert r.returncode == 7, (r.returncode, r.stdout, r.stderr)
    assert "could not read CONVICTION_EARLY_LEAD_MINUTES" in r.stderr
