"""The watchdog must ship executable. No `chmod` step on the activation path.

`recompute-coverage-watchdog.service` runs `ExecStart` against the script IN
THE REPO -- deliberately, because installing the `.sh` to `/usr/local/bin`
while it invokes the `.py` from the repo splits the watchdog in half, and that
exact trap is already in this project's history with the backup scripts.

Which makes the executable bit mandatory rather than cosmetic. It shipped
`100644` with a `chmod +x` line in the runbook, and an operator who skipped
that line got a unit that fails on permissions: the timer fires, the watchdog
never runs, and nothing says so -- the deploy-without-activate failure the
runbook section exists to close, reachable by skipping one line of it.

It also left the production checkout permanently dirty (`M` on a pure mode
change), so real drift was camouflaged among expected noise.

Asserted against the git INDEX, not the filesystem: a Windows working tree
does not carry the bit, so checking `os.access(X_OK)` would pass or fail for
reasons unrelated to what actually ships.
"""

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Scripts a systemd unit executes directly from the repo.
EXECUTED_FROM_REPO = ["scripts/recompute-coverage-watchdog.sh"]


def _mode(path: str) -> str:
    r = subprocess.run(
        ["git", "ls-files", "--stage", path],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    assert r.returncode == 0 and r.stdout.strip(), f"{path} is not tracked: {r.stderr}"
    return r.stdout.split()[0]


@pytest.mark.parametrize("path", EXECUTED_FROM_REPO)
def test_a_script_systemd_runs_from_the_repo_ships_executable(path):
    assert _mode(path) == "100755", (
        f"{path} ships mode {_mode(path)}, but a systemd ExecStart runs it "
        "directly from the repo. Without the executable bit the unit fails on "
        "permissions and the watchdog silently never runs. Fix with "
        f"`git update-index --chmod=+x {path}` -- do NOT add a chmod step to "
        "the runbook, which is what this replaced."
    )


def test_the_runbook_does_not_reintroduce_a_chmod_step():
    """The fix is removing the manual step, not documenting it."""
    runbook = (REPO_ROOT / "docs" / "runbook_recompute_coverage.md").read_text(
        encoding="utf-8"
    )
    activation = runbook.split("systemctl enable --now")[0]
    assert "chmod +x scripts/recompute-coverage-watchdog.sh" not in activation, (
        "a chmod step is back on the activation path; the script ships 100755 "
        "so the step is both unnecessary and skippable"
    )
