"""The off-host shipper must actually be TRIGGERED, and its credentials must not
ride on the command line or reach a network-facing process.

Why this file exists: the shipper, its verification and its §12a watchdog check
all shipped together and nothing ran the job. Repo-wide, ``gecko-backup-offhost``
appeared only in its own self-references and in the runbook. Follow the enable
sequence exactly and the heartbeat gets written once, by hand, goes stale, and
48h later check 6 pages every cooldown window forever over a lane nothing runs.
A watchdog that cries wolf is worse than no watchdog: it teaches the operator to
skim past the page that was real.

That class of gap is invisible to every behavioural test — the script works, the
watchdog works, the wiring between them and the trigger is simply absent — so it
needs a contract test over the deployed units.

The first attempt at that wiring was a cron entry 30 minutes after the backup
timer, and it was WRONG in a way this file now pins against. ``gecko-backup.timer``
carries ``AccuracySec=1h``, so the backup may start anywhere in 03:00-04:00;
``Persistent=true`` also fires missed runs at boot; and the backup legitimately
runs for many minutes (``TimeoutStartSec=1800``, set because 600s expired
mid-``.backup`` on a 6.82 GB database). On any deferred day a fixed-time shipper
re-verifies YESTERDAY's backup and writes a fresh heartbeat — a lane that reads
healthy while being permanently one day stale, which no behavioural test can see
because every individual run succeeds.

So the guarantee asserted here is EVENT ordering, not a clock gap: the shipper is
triggered by ``gecko-backup.service`` completing successfully.

Pure-stdlib and platform-independent: it reads files, runs nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CRONTAB = REPO_ROOT / "cron" / "gecko-alpha.crontab"
RUNBOOK = REPO_ROOT / "docs" / "runbook_offhost_backup_b2.md"
SCRIPT = REPO_ROOT / "scripts" / "gecko-backup-offhost.sh"
BACKUP_UNIT = REPO_ROOT / "systemd" / "gecko-backup.service"
SHIPPER_UNIT = REPO_ROOT / "systemd" / "gecko-backup-offhost.service"
DASHBOARD_UNIT = REPO_ROOT / "systemd" / "gecko-dashboard.service"

ENV_FILE = "/etc/gecko-alpha/offhost.env"


def _directives(path: Path) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


# ---------------------------------------------------------------------
# The shipper is triggered at all
# ---------------------------------------------------------------------


def test_the_shipper_unit_exists():
    assert SHIPPER_UNIT.is_file(), "no systemd unit runs the off-host shipper"


def test_the_backup_unit_triggers_the_shipper_on_success():
    """The whole point. Without this line the lane is code nobody runs."""
    directives = _directives(BACKUP_UNIT)
    assert "OnSuccess=gecko-backup-offhost.service" in directives, (
        "gecko-backup.service does not trigger the shipper; the off-host lane "
        f"would never run. Directives: {directives}"
    )


def test_the_trigger_is_success_only_not_unconditional():
    """A failed backup must ship nothing. Firing regardless would re-confirm
    YESTERDAY's copy as though it were today's and write a fresh heartbeat over
    a backup that was never made."""
    body = BACKUP_UNIT.read_text(encoding="utf-8")
    assert "OnFailure=gecko-backup-offhost.service" not in body
    assert "OnSuccess=gecko-backup-offhost.service" in body


def test_the_shipper_is_ordered_after_the_backup():
    assert "After=gecko-backup.service" in _directives(SHIPPER_UNIT)


def test_the_shipper_runs_the_installed_script():
    directives = _directives(SHIPPER_UNIT)
    assert any(
        d.startswith("ExecStart=") and d.endswith("gecko-backup-offhost.sh")
        for d in directives
    ), directives
    # /usr/local/bin, matching the rest of the backup stack — a repo path here
    # would silently diverge from what the other units execute.
    assert any(
        "ExecStart=/usr/local/bin/gecko-backup-offhost.sh" == d for d in directives
    ), directives


def test_the_shipper_is_not_clock_coupled():
    """No timer, and no cron entry. The ordering guarantee is event-based; a
    second, time-based trigger would reintroduce exactly the staleness this
    design removes (and double-run into the flock)."""
    assert not (REPO_ROOT / "systemd" / "gecko-backup-offhost.timer").exists(), (
        "a timer would reintroduce clock coupling alongside the OnSuccess trigger"
    )
    active_cron = [
        line
        for line in CRONTAB.read_text(encoding="utf-8").splitlines()
        if line.strip()
        and not line.lstrip().startswith("#")
        and "gecko-backup-offhost" in line
    ]
    assert active_cron == [], f"shipper is also scheduled by cron: {active_cron}"


# ---------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------


def test_the_shipper_unit_loads_the_credential_file_optionally():
    directives = _directives(SHIPPER_UNIT)
    assert f"EnvironmentFile=-{ENV_FILE}" in directives, (
        "the shipper must load its configuration from the 0600 env file, and "
        "the leading '-' must make a missing file a no-op so the unit is safe "
        f"to install before the lane is configured. Directives: {directives}"
    )


def test_no_credential_value_is_inlined_in_any_unit_or_the_crontab():
    """*** The credential must never reach a command line or this repo. ***

    A unit's environment is readable via ``systemctl show`` and /proc, and these
    files are committed — an inlined key would be a live credential in git
    history. The script goes out of its way to keep the key out of argv; putting
    it in the trigger would hand it straight back.
    """
    for path in (CRONTAB, SHIPPER_UNIT, BACKUP_UNIT, DASHBOARD_UNIT):
        text = path.read_text(encoding="utf-8")
        for var in (
            "GECKO_OFFHOST_S3_APPLICATION_KEY",
            "GECKO_OFFHOST_S3_KEY_ID",
            "GECKO_OFFHOST_S3_ENDPOINT",
            "GECKO_OFFHOST_S3_BUCKET",
        ):
            hit = re.search(rf"^\s*[^#\n]*\b{var}=(\S+)", text, re.M)
            assert hit is None, (
                f"{path.name} assigns {var} a value ({hit.group(1)!r}); "
                f"credentials belong in {ENV_FILE} at mode 0600"
            )


def test_the_dashboard_unit_never_loads_the_credential_file():
    """*** Load-bearing, and the reason the marker file exists. ***

    The dashboard needs to know WHETHER the off-host lane is configured. The
    obvious fix — adding the shipper's EnvironmentFile to this unit — would put
    the B2 application key into a network-facing process's environment to
    correct a display field, trading a wrong dashboard for a credential
    exposure. The shipper publishes a non-secret marker instead.
    """
    body = DASHBOARD_UNIT.read_text(encoding="utf-8")
    assert ENV_FILE not in body, (
        "gecko-dashboard.service loads the off-host credential file; a "
        "network-facing process must not hold the B2 application key"
    )
    assert "EnvironmentFile" not in body


def test_the_shipper_publishes_a_non_secret_configured_marker():
    """The cross-process signal the dashboard reads instead of the env."""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "GECKO_OFFHOST_CONFIGURED_MARKER" in body
    assert "_write_configured_marker" in body
    assert "_clear_configured_marker" in body
    api = (REPO_ROOT / "dashboard" / "api.py").read_text(encoding="utf-8")
    assert "GECKO_OFFHOST_CONFIGURED_MARKER" in api, (
        "the dashboard does not read the marker, so offhost_configured still "
        "depends on env vars that never reach its process"
    )


# ---------------------------------------------------------------------
# Safe to deploy before the lane is configured
# ---------------------------------------------------------------------


def test_the_trigger_is_inert_until_configured():
    """Installing the units before any credential exists must be safe, since
    that ordering is what proves the plumbing before the watch is enabled."""
    body = SCRIPT.read_text(encoding="utf-8")
    assert "off-host backup disabled" in body
    disabled_block = body.split("off-host backup disabled", 1)[1][:300]
    assert "exit 0" in disabled_block, disabled_block


def test_runbook_puts_the_trigger_before_enabling_the_watch():
    """Ordering is the whole mitigation: install the trigger, observe it no-op,
    then turn on the watchdog. Documented the other way round, an operator
    produces a guaranteed false-page loop on day one."""
    text = RUNBOOK.read_text(encoding="utf-8")
    step0 = text.find("Step 0")
    watch_on = text.find("OFFHOST_BACKUP_WATCH_ENABLED=true")
    assert step0 != -1, "the runbook has no Step 0"
    assert watch_on != -1, "the runbook never says to enable the watch"
    assert step0 < watch_on, (
        "the runbook enables the watchdog before it installs the trigger"
    )


def test_runbook_documents_the_event_trigger_not_a_clock_gap():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "OnSuccess" in text, (
        "the runbook must document how the shipper is triggered, or the "
        "operator will install a timer and reintroduce the staleness"
    )


def test_runbook_forbids_inlining_the_key_in_an_ssh_command():
    """The earlier draft told the operator to run
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


def test_runbook_requires_verifying_rclone_exit_codes_on_the_box():
    """The absent-vs-unreachable mapping is taken from rclone's documentation.
    Documentation is not the running program, and getting it wrong means the
    shipper tells the operator a present backup is missing."""
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "exit code" in text.lower()
    assert "lsjson" in text


# ---------------------------------------------------------------------
# The operator's frozen B2 specification
# ---------------------------------------------------------------------
#
# These are binding values, not examples. They are asserted here so a later
# edit cannot quietly return the runbook to generic "create a bucket" wording,
# and so the deliberate-looking-wrong choices (Object Lock OFF, write
# capability on a backup uploader) keep their recorded reasons.


def _runbook():
    return RUNBOOK.read_text(encoding="utf-8")


def test_runbook_pins_the_exact_bucket_name():
    assert "gecko-alpha-srilu-prod-backups-a81626" in _runbook()


def test_runbook_pins_the_exact_key_name_and_prefix():
    text = _runbook()
    assert "gecko-alpha-srilu-offhost" in text
    assert "GECKO_OFFHOST_S3_PREFIX=hosts/srilu" in text


def test_runbook_records_object_lock_off_with_its_reason():
    """Object Lock OFF looks like an oversight to anyone hardening the bucket
    later. The reason has to travel with the value: this lane deletes staging
    keys and provably-wrong objects, and promotes by server-side move."""
    text = _runbook()
    assert "Object Lock" in text
    assert "OFF for v1" in text
    lock_section = text.split("Object Lock is off deliberately", 1)
    assert len(lock_section) == 2, "no rationale recorded for Object Lock OFF"


def test_runbook_says_the_master_key_does_not_work():
    """Otherwise the first instinct during bring-up is to 'just use the master
    key to get started', which is not a shortcut that exists for the S3 API."""
    text = _runbook()
    assert "master key" in text
    assert "manually" in text.lower()


def test_runbook_justifies_write_capability_and_list_all_bucket_names():
    text = _runbook()
    assert "listAllBucketNames" in text
    assert "Read" in text and "Write" in text
    # The justification, not just the setting.
    assert "read-only key would fail" in text.lower() or "Read alone cannot" in text


def test_runbook_states_bucket_names_are_globally_unique_and_that_this_is_not_policy():
    """A name collision must not be read as license to change the spec."""
    text = _runbook()
    assert "globally unique" in text
    assert "not a policy" in text.lower()


def test_runbook_forbids_the_secret_in_chat_pr_argv_or_cron():
    text = _runbook().lower()
    for forbidden in ("chat", "pr", "cron"):
        assert forbidden in text, forbidden
    assert "shown once" in text or "displayed" in text
    assert "0600" in _runbook()
    assert "root:root" in _runbook()


def test_runbook_lists_exactly_the_six_env_keys():
    text = _runbook()
    for key in (
        "GECKO_OFFHOST_BACKUP_TRANSPORT=s3",
        "GECKO_OFFHOST_S3_BUCKET=",
        "GECKO_OFFHOST_S3_ENDPOINT=",
        "GECKO_OFFHOST_S3_KEY_ID=",
        "GECKO_OFFHOST_S3_APPLICATION_KEY=",
        "GECKO_OFFHOST_S3_PREFIX=hosts/srilu",
    ):
        assert key in text, key


def test_runbook_has_a_six_step_activation_gate():
    """Every step is a thing that has to be TRUE before the watchdog is armed,
    and the ordering is the mitigation: probe and verify before uploading, prove
    the restore before trusting the lane."""
    text = _runbook()
    gate = text.split("## ACTIVATION GATE", 1)
    assert len(gate) == 2, "no activation gate section"
    body = gate[1].split("\n## ", 1)[0]
    checkboxes = [ln for ln in body.splitlines() if ln.strip().startswith("- [ ]")]
    assert len(checkboxes) == 6, f"expected 6 gate items, found {len(checkboxes)}"
    # Assert content against the WHOLE gate body, not just the first line of
    # each item — the load-bearing detail (which URI a quick_check must use)
    # lives on continuation lines.
    lowered = body.lower()
    assert "5 gib" in lowered or "5gib" in lowered
    assert "exit code" in lowered
    assert "hash" in lowered
    assert "quick_check" in lowered
    assert "immutable=1" in lowered


def test_activation_gate_precedes_enabling_the_watch_flag():
    text = _runbook()
    gate = text.find("## ACTIVATION GATE")
    # The LAST mention of arming the watchdog must come after the gate.
    arm = text.rfind("OFFHOST_BACKUP_WATCH_ENABLED")
    assert gate != -1 and arm != -1
    assert gate < arm, "the gate is documented after the flag it guards"


def test_runbook_repeats_the_usr_local_bin_trap_in_the_gate():
    """`git pull` deploying nothing is the single most repeated deployment
    mistake in this repo; it belongs where the operator is actually working."""
    text = _runbook()
    gate_onward = text.split("## ACTIVATION GATE", 1)[1]
    assert "/usr/local/bin" in gate_onward
    assert "git pull" in gate_onward
