"""Structural guard: every script invoked BY PATH from cron or systemd must
carry the executable bit in git.

Root cause this enforces (2026-07-03): cron/systemd invoke a script by path
(`/root/gecko-alpha/scripts/X.sh`), which requires the +x bit. A script
committed mode 0644 fails `Permission denied` every scheduled run, silently —
the held-position/revival/acceleration watchdogs were all dark this way (the
last for ~31 days). Verifying with `bash script.sh` bypasses the bit and gives
a false PASS; this test asserts the committed git mode instead, so the class is
enforced, not remembered. See tasks/lessons.md.
"""

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _cron_invoked_scripts() -> set[str]:
    """Script paths invoked directly (by path) from the managed crontab +
    systemd unit ExecStart lines. A path is 'direct' if the command token is
    the script itself (not `bash script` / `python script`)."""
    paths: set[str] = set()
    sources = list((REPO / "cron").glob("*.crontab")) + list(
        (REPO / "systemd").glob("*.service")
    )
    for f in sources:
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            for tok in line.split():
                # a repo-relative script path invoked directly
                if tok.endswith(".sh") and "/scripts/" in tok:
                    # prior token must NOT be an interpreter (bash/sh/python)
                    idx = line.split().index(tok)
                    prev = line.split()[idx - 1] if idx > 0 else ""
                    if Path(prev).name not in {"bash", "sh", "python", "python3"}:
                        paths.add(tok.split("/scripts/")[-1])
    return paths


def test_cron_invoked_scripts_are_executable_in_git():
    """ASSERTION: for every script invoked by path from cron/systemd, its
    git-tracked mode is 100755 (executable). Mode 100644 → cron Permission
    denied → silent dark watchdog."""
    names = _cron_invoked_scripts()
    assert names, "no cron/systemd-invoked scripts discovered — parser regression?"
    non_exec = []
    for name in sorted(names):
        rel = f"scripts/{name}"
        out = subprocess.run(
            ["git", "ls-files", "-s", rel],
            cwd=REPO,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not out:
            continue  # not tracked (installed at deploy) — skip
        mode = out.split()[0]
        if mode != "100755":
            non_exec.append(f"{rel} is git mode {mode} (need 100755)")
    assert not non_exec, "cron/systemd-invoked scripts missing +x:\n" + "\n".join(
        non_exec
    )
