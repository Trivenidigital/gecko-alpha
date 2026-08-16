"""The off-host shipper must actually be SCHEDULED, and its credentials must not
ride on the command line.

Why this file exists: the shipper, its verification and its §12a watchdog check
all shipped together and were individually well covered — and nothing anywhere
ran the job. Repo-wide, ``gecko-backup-offhost`` appeared only in its own
self-references and in the runbook. Follow the enable sequence exactly and the
heartbeat gets written once, by hand, goes stale, and 48h later check 6 pages
every cooldown window forever over a lane nothing runs. A watchdog that cries
wolf is worse than no watchdog: it teaches the operator to skim past the page
that was real.

That class of gap is invisible to every behavioural test — the script works, the
watchdog works, the wiring between them and the clock is simply absent — so it
needs a contract test over the deployed cron file.

Pure-stdlib and platform-independent: it reads files, runs nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CRONTAB = REPO_ROOT / "cron" / "gecko-alpha.crontab"
RUNBOOK = REPO_ROOT / "docs" / "runbook_offhost_backup_b2.md"
SCRIPT = REPO_ROOT / "scripts" / "gecko-backup-offhost.sh"

ENV_FILE = "/etc/gecko-alpha/offhost.env"


def _cron_lines():
    """Active (non-comment, non-blank) crontab entries."""
    return [
        line
        for line in CRONTAB.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _shipper_lines():
    return [line for line in _cron_lines() if "gecko-backup-offhost.sh" in line]


def test_the_shipper_is_actually_scheduled():
    """The whole point. Without this line the lane is code nobody runs."""
    lines = _shipper_lines()
    assert len(lines) == 1, (
        "expected exactly one ACTIVE crontab entry invoking "
        f"gecko-backup-offhost.sh, found {len(lines)}: {lines}"
    )


def test_the_schedule_is_a_real_daily_time():
    """A five-field schedule, not a comment that looks like one."""
    line = _shipper_lines()[0]
    fields = line.split()
    assert len(fields) >= 6, f"not a cron entry: {line}"
    minute, hour = fields[0], fields[1]
    assert re.fullmatch(r"\d+", minute), f"minute field is not fixed: {minute}"
    assert re.fullmatch(r"\d+", hour), f"hour field is not fixed: {hour}"
    assert fields[2:5] == ["*", "*", "*"], f"expected a daily schedule: {line}"


def test_the_shipper_runs_after_the_local_backup():
    """It ships what the 03:00 UTC backup+rotate timer just produced. Running
    before it would ship yesterday's backup every day — a lane that looks
    healthy while being permanently one day stale."""
    fields = _shipper_lines()[0].split()
    minutes_past_midnight = int(fields[1]) * 60 + int(fields[0])
    assert 3 * 60 < minutes_past_midnight <= 6 * 60, (
        "the shipper should run shortly AFTER the 03:00 UTC backup timer, "
        f"got {fields[1]}:{fields[0]} UTC"
    )


def test_the_cron_line_sources_the_credential_file():
    line = _shipper_lines()[0]
    assert ENV_FILE in line, (
        f"the cron line must source {ENV_FILE}; without it the shipper runs "
        f"with no configuration and no-ops forever: {line}"
    )


def test_no_credential_value_is_inlined_in_the_crontab():
    """*** The credential must never reach a command line. ***

    A cron entry's environment is readable via ``ps auxwwe`` and /proc by anyone
    on the box, and this file is committed to the repo — an inlined key would be
    a live credential in git history. The script goes out of its way to keep the
    key out of argv; putting it in the schedule would hand it back.
    """
    text = CRONTAB.read_text(encoding="utf-8")
    for var in (
        "GECKO_OFFHOST_S3_APPLICATION_KEY",
        "GECKO_OFFHOST_S3_KEY_ID",
        "GECKO_OFFHOST_S3_ENDPOINT",
        "GECKO_OFFHOST_S3_BUCKET",
    ):
        assigned = re.search(rf"^\s*[^#\n]*\b{var}=(\S+)", text, re.M)
        assert assigned is None, (
            f"{var} is assigned a value in the crontab ({assigned.group(1)!r}); "
            f"credentials belong in {ENV_FILE} at mode 0600, sourced by the line"
        )


def test_the_scheduled_line_is_inert_until_configured():
    """Deploying the schedule before any credential exists must be safe, since
    that ordering is what proves the plumbing before the watch is enabled. The
    script's disabled path is the guarantee; assert it is still there."""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "off-host backup disabled" in body
    # Reached by an exit 0, not an error path.
    disabled_block = body.split("off-host backup disabled", 1)[1][:200]
    assert "exit 0" in disabled_block, disabled_block


def test_runbook_puts_scheduling_before_enabling_the_watch():
    """Ordering is the whole mitigation: schedule, observe it no-op, then turn
    on the watchdog. Documented the other way round, an operator produces a
    guaranteed false-page loop on day one."""
    text = RUNBOOK.read_text(encoding="utf-8")
    step0 = text.find("Step 0")
    watch_on = text.find("OFFHOST_BACKUP_WATCH_ENABLED=true")
    assert step0 != -1, "the runbook has no Step 0"
    assert watch_on != -1, "the runbook never says to enable the watch"
    assert step0 < watch_on, (
        "the runbook enables the watchdog before it schedules the shipper"
    )


def test_runbook_forbids_inlining_the_key_in_an_ssh_command():
    """The earlier draft of this runbook told the operator to run
    ``ssh host 'GECKO_OFFHOST_S3_APPLICATION_KEY=<key> ... script.sh'``, which
    lands the secret in local shell history, local ssh argv, and the remote's
    argv and environ. Documentation that contradicts the code's own threat model
    is the version the operator actually follows."""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert not re.search(
        r"ssh\s+\S+\s+'[^']*GECKO_OFFHOST_S3_APPLICATION_KEY=", text
    ), "the runbook inlines the application key into an ssh command line"
    assert ENV_FILE in text, "the runbook must direct the key to the 0600 env file"
    assert "0600" in text or "umask 077" in text
