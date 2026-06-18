"""Stale-reader watchdog (P0 Part B). Pure-logic tests run everywhere; the
/proc scan integration test needs symlink support (skipped on Windows w/o it)."""

import os
import time

import pytest

from scout.observability.sqlite_holder_watchdog import (
    DbHolder,
    _read_btime,
    _start_epoch,
    classify_is_expected_service,
    find_stale_readers,
    scan_db_holders,
)

GECKO_UNITS = ["gecko-pipeline.service", "gecko-dashboard.service"]


def _symlinks_supported(tmp_path) -> bool:
    probe = tmp_path / "_lnkprobe"
    try:
        os.symlink("target", probe)
        os.remove(probe)
        return True
    except OSError:
        return False


def _mk_proc(tmp_path, btime, pids):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(f"cpu  0 0 0\nbtime {int(btime)}\nprocesses 1\n")
    for pid, (target, cmd, cgroup, starttime_ticks) in pids.items():
        d = proc / str(pid)
        (d / "fd").mkdir(parents=True)
        os.symlink(target, d / "fd" / "6")
        (d / "cmdline").write_bytes(cmd.encode() + b"\x00")
        (d / "cgroup").write_text(cgroup)
        # /proc/<pid>/stat: "pid (comm) state <fields 4..>"; starttime is field 22,
        # i.e. index 19 of the whitespace split *after* the closing paren.
        after_paren = ["S"] + ["0"] * 18 + [str(int(starttime_ticks))]
        (d / "stat").write_text(f"{pid} (python3) " + " ".join(after_paren) + "\n")
    return str(proc)


# ---- pure logic (runs on all platforms) ----


def test_classify_expected_service_allowlist():
    assert classify_is_expected_service(
        "0::/system.slice/gecko-pipeline.service", GECKO_UNITS
    )
    assert classify_is_expected_service(
        "0::/system.slice/gecko-dashboard.service", GECKO_UNITS
    )
    # Fold 3: an unexpected systemd unit is NOT treated as expected.
    assert not classify_is_expected_service(
        "0::/system.slice/cron.service", GECKO_UNITS
    )
    assert not classify_is_expected_service(
        "0::/user.slice/user-0.slice/session-7.scope", GECKO_UNITS
    )


def test_find_stale_readers_filters_expected_own_and_young():
    def H(pid, age_h, expected):
        return DbHolder(pid, "x", "cg", age_h * 3600, expected)

    holders = [
        H(99, 7, False),  # old orphan -> stale
        H(98, 9, True),  # old but expected service -> excluded
        H(1, 9, False),  # own pid -> excluded
        H(97, 1, False),  # young -> excluded
    ]
    stale = find_stale_readers(holders, max_age_hours=6.0, own_pid=1)
    assert [h.pid for h in stale] == [99]


def test_find_stale_readers_flags_rogue_service(tmp_path):
    """Fold 3: a long-lived holder under a non-allowlisted service is flagged."""
    rogue = DbHolder(
        555, "python3 cron_job.py", "0::/system.slice/cron.service", 9 * 3600, False
    )
    stale = find_stale_readers([rogue], max_age_hours=6.0, own_pid=1)
    assert [h.pid for h in stale] == [555]


def test_start_epoch_and_btime_parse(tmp_path):
    btime = 1_000_000.0
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "stat").write_text(f"cpu 0 0\nbtime {int(btime)}\n")
    d = proc / "4242"
    d.mkdir()
    after_paren = ["S"] + ["0"] * 18 + ["50000"]  # starttime = 50000 ticks
    (d / "stat").write_text("4242 (python3) " + " ".join(after_paren) + "\n")
    assert _read_btime(str(proc)) == btime
    # start_epoch = btime + starttime/clk_tck = 1_000_000 + 50000/100 = 1_000_500
    assert _start_epoch(str(proc), 4242, btime, 100) == 1_000_500.0


def test_scan_missing_proc_returns_empty(tmp_path):
    assert (
        scan_db_holders([str(tmp_path / "scout.db")], proc_root=str(tmp_path / "nope"))
        == []
    )


# ---- /proc scan integration (needs symlinks) ----


def test_scan_detects_db_holder(tmp_path):
    if not _symlinks_supported(tmp_path):
        pytest.skip("symlinks unsupported on this platform")
    db = str(tmp_path / "scout.db")
    open(db, "w").close()
    now = time.time()
    btime = now - 100_000  # boot 100k s ago
    proc = _mk_proc(
        tmp_path,
        btime,
        {4242: (db, "python3 _report.py", "0::/user.slice/session-7.scope", 50_000)},
    )
    holders = scan_db_holders(
        [db],
        proc_root=proc,
        own_pid=1,
        now=now,
        clk_tck=100,
        expected_units=GECKO_UNITS,
    )
    assert len(holders) == 1
    h = holders[0]
    assert h.pid == 4242
    assert h.is_expected_service is False
    assert h.cmdline == "python3 _report.py"
    # age = now - (btime + 50000/100) = 100000 - 500 = 99500
    assert abs(h.age_seconds - 99_500) < 2


def test_scan_excludes_own_pid_and_expected_service(tmp_path):
    if not _symlinks_supported(tmp_path):
        pytest.skip("symlinks unsupported on this platform")
    db = str(tmp_path / "scout.db")
    open(db, "w").close()
    now = time.time()
    btime = now - 100_000
    proc = _mk_proc(
        tmp_path,
        btime,
        {
            7: (
                db,
                "python3 -m scout.main",
                "0::/system.slice/gecko-pipeline.service",
                10,
            ),
            9: (db, "python3 _report.py", "0::/user.slice/session-9.scope", 10),
        },
    )
    holders = scan_db_holders(
        [db],
        proc_root=proc,
        own_pid=7,
        now=now,
        clk_tck=100,
        expected_units=GECKO_UNITS,
    )
    # own pid 7 excluded; pid 9 present and not expected-service
    assert [h.pid for h in holders] == [9]
    assert holders[0].is_expected_service is False
