"""Round 6 static lint: every managed-block cron line redirects stdout+stderr.

On srilu, the local MTA is not configured. cron's default behavior on job
exit is to mail any stderr/stdout to the user — so an unredirected line
drops failures on the floor (`/var/mail/root` doesn't exist). Per-job log
files (`/var/log/gecko-alpha-*.log`) give the operator a journalctl-
equivalent audit trail for bash workloads.

This test reads `cron/gecko-alpha.crontab` and asserts that every line
between the sentinel markers ends with `>> /var/log/... 2>&1`.
"""

from __future__ import annotations

import re
from pathlib import Path


CRONTAB = Path(__file__).resolve().parent.parent / "cron" / "gecko-alpha.crontab"
BEGIN_MARK = "# === BEGIN gecko-alpha managed block (do not edit between sentinels) ==="
END_MARK = "# === END gecko-alpha managed block ==="


def _managed_lines() -> list[tuple[int, str]]:
    text = CRONTAB.read_text(encoding="utf-8")
    inside = False
    out: list[tuple[int, str]] = []
    for i, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s == BEGIN_MARK:
            inside = True
            continue
        if s == END_MARK:
            inside = False
            continue
        if inside and s and not s.startswith("#"):
            out.append((i, line))
    return out


def test_managed_block_is_present_and_non_empty():
    lines = _managed_lines()
    assert lines, (
        f"cron/gecko-alpha.crontab managed block (between {BEGIN_MARK!r} and "
        f"{END_MARK!r}) is empty"
    )


def test_every_managed_cron_line_redirects_stdout_and_stderr():
    pat = re.compile(r">>\s+/var/log/gecko-alpha-\S+\.log\s+2>&1\s*$")
    offenders: list[tuple[int, str]] = []
    for lineno, line in _managed_lines():
        if not pat.search(line):
            offenders.append((lineno, line))
    assert not offenders, (
        "cron/gecko-alpha.crontab managed-block lines lacking the "
        "`>> /var/log/gecko-alpha-<name>.log 2>&1` redirection (failures "
        "would vanish since srilu has no MTA configured):\n"
        + "\n".join(f"  - line {ln}: {body}" for ln, body in offenders)
    )
