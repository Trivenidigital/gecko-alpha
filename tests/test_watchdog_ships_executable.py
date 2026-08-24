"""Every script invoked by repo path must ship executable. Derived, not listed.

Deployment here is `git pull` into `/root/gecko-alpha`, and systemd units and
cron lines invoke scripts *out of that checkout* — deliberately, because
installing a `.sh` to `/usr/local/bin` while it invokes a `.py` from the repo
splits a watchdog in half, a trap already in this project's history with the
backup scripts.

Which makes the executable bit a deploy-path precondition rather than a
cosmetic detail. Miss it and the unit fails on permissions: the timer fires,
the watchdog never runs, and nothing says so — the deploy-without-activate
failure these watchdogs exist to close, reachable by shipping one wrong mode.

THE LIST IS DERIVED FROM THE PRODUCTION ARTEFACTS, and that is the whole point
of this file. The first version hard-coded a single path while its own
docstring and test name asserted the general property "scripts a systemd unit
executes directly from the repo". Four scripts satisfied that predicate and it
covered one. A reviewer demonstrated the gap by clearing the bit on
`systemd-drift-watchdog.sh` — 100644, and both tests plus the whole watchdog
suite stayed green. That one is the worst to lose, because its unit is the only
one of the four without an `ExecStartPre=/usr/bin/test -x` fallback, so it fails
in exactly the silent way this file exists to prevent.

Seventh instance on this PR of a fix closing one BRANCH and not the AXIS — and
this PR had already solved it once, in
`tests/test_recompute_mark_rises_on_every_surface.py`, by parametrising over
`Database._RECOMPUTE_SURFACES` rather than a literal. Same remedy here: parse
the units and the cron docs, and a fifth invocation added later is covered the
day it lands.

Asserted against the git INDEX, not the filesystem. A Windows working tree does
not carry the executable bit, so `os.access(X_OK)` would pass or fail for
reasons unrelated to what ships; the index is what `git pull` writes, so index
mode IS the shipped property, and it is the only spelling that measures the
same thing on both platforms.
"""

import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Repo-rooted `.sh` invocations, as they appear in deployed artefacts.
_REPO_SH = re.compile(r"/root/gecko-alpha/(scripts/[A-Za-z0-9_.-]+\.sh)")


def _invoked_from_repo() -> dict[str, list[str]]:
    """{script path -> the artefacts that invoke it by repo path}."""
    found: dict[str, list[str]] = {}
    globs = ("scripts/*.service", "scripts/*.timer", "systemd/*", "cron/*")
    for pattern in globs:
        for artefact in REPO_ROOT.glob(pattern):
            if artefact.is_dir():
                continue
            try:
                text = artefact.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for match in _REPO_SH.findall(text):
                found.setdefault(match, []).append(artefact.name)
    return found


def _index_mode(path: str) -> str:
    r = subprocess.run(
        ["git", "ls-files", "--stage", path],
        cwd=str(REPO_ROOT), capture_output=True, text=True,
    )
    if r.returncode != 0 or not r.stdout.strip():
        return "UNTRACKED"
    return r.stdout.split()[0]


INVOKED = _invoked_from_repo()


def test_the_derivation_finds_the_known_invocations():
    """Guard the guard: an empty or shrunken derivation would pass everything.

    A parser that silently matches nothing turns every assertion below into a
    vacuous pass -- the failure mode this file was written to fix, one level up.
    """
    assert len(INVOKED) >= 4, (
        f"the derivation found only {len(INVOKED)} repo-path invocations; "
        "it has stopped matching the deployed artefacts"
    )
    assert "scripts/recompute-coverage-watchdog.sh" in INVOKED
    assert "scripts/systemd-drift-watchdog.sh" in INVOKED


@pytest.mark.parametrize("script", sorted(INVOKED))
def test_a_script_invoked_by_repo_path_ships_executable(script):
    mode = _index_mode(script)
    assert mode == "100755", (
        f"{script} ships mode {mode}, but {', '.join(sorted(set(INVOKED[script])))} "
        "invokes it by repo path. Without the executable bit it fails on "
        f"permissions and nothing reports it. Fix with `git update-index "
        f"--chmod=+x {script}` -- do NOT add a chmod step to the runbook, "
        "which is what this replaced."
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
