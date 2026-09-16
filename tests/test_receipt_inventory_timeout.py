"""Synthetic Linux proof of the receipt wrapper supervisor, never production collection.

Process tree per case (design revision 7, tasks/design_receipt_cleanup_replacement_2026_09_16.md):

    pytest (never signals a group)
     └─ P  case runner   --case CASE DIR     subreaper; deadline, recovery, bounded relay
         └─ W worker     --worker CASE DIR   subreaper; spawns S, identity, assertions
             └─ S supervisor                 scripts/receipt_inventory_supervisor.py
                 └─ L timeout -k 2 10 bash … start_new_session; sid == pgid == L.pid == G
                     └─ bash → producer | head | reader

The negative control and the ``--raw-wrapper`` leak controls keep the original
raw path (W spawns the wrapper directly) so the pre-supervisor failure remains
a positive characterization. Windows skips are discovery checks only.
"""

import ctypes
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections import namedtuple


HERE = Path(__file__).resolve()
ROOT = HERE.parent.parent
SUPERVISOR = ROOT / "scripts" / "receipt_inventory_supervisor.py"
FAULT_RUNNER = HERE.parent / "receipt_supervisor_faults.py"

TIMEOUT_EXITS = (124, -9, 137)
FIXED_OUTPUT = b"receipt-inventory-fixed-output\n"
READINESS_SECONDS = 4.0
TICK = 0.02
DRAIN_READS_PER_TICK = 4
DRAIN_BYTES_PER_TICK = 65_536
WORKER_RETAIN = 131_073
RUNNER_RETAIN = 1_048_576
RELAY_INCOMPLETE_EXIT = 12
DIAGNOSTIC_BUDGET = 65_536  # design 5.4: W's own diagnostic prints are bounded to 64 KiB
WRITE_RETRY_SECONDS = 0.005
PROC = "/proc"
STATUS_KEYS = (
    "status", "exit_code", "pgid", "inner_exit", "inner_elapsed", "total_elapsed",
    "reaped", "kills", "signals_received", "signal_phase", "teardowns",
    "cleanup_proof", "dropped", "overflow", "error", "survivors", "output",
)

# case -> (fixture, fault, extra supervisor options)
CASES = {
    "silent": ("silent", None, ()),
    "stderr_flood": ("stderr_flood", None, ()),
    "ignore_term": ("ignore_term", None, ()),
    "descendant": ("descendant", None, ()),
    "continuous_flood": ("continuous_flood", None, ()),
    "clean_success": ("clean_success", None, ()),
    "command_failed": ("command_failed", None, ()),
    "early_exit_orphan": ("early_exit_orphan", None, ()),
    "output_flood": ("output_flood", None, ()),
    # Fast class: S's own partitions are shortened so its report deadline
    # (T0+3.5) precedes the worker's 8 s deadline; the mechanism is unchanged.
    "stalled_reader": ("clean_success", None, ("--wait", "2", "--cleanup", "1", "--report", "0.5")),
    "closed_reader": ("clean_success", None, ()),
    "publish_error": ("ignore_term", "publish_error", ()),
    "die_before_publish": ("ignore_term", "die_before_publish", ()),
    "capture_fault": ("ignore_term", "capture_error", ()),
    "injected_startup": ("ignore_term", "inject_signal:startup:1", ()),
    "injected_cleanup_repeat": ("early_exit_orphan", "inject_signal:cleanup:2", ()),
    "hup_mid_run": ("ignore_term", None, ()),
    "term_mid_run": ("ignore_term", None, ()),
    "outer_deadline": ("ignore_term", None, ("--wait", "3")),
    "stalled_supervisor": ("ignore_term", "stall_wait", ()),
    "stall_cleanup": ("early_exit_orphan", "stall_cleanup", ()),
    "parent_live_supervisor": ("ignore_term", "stall_wait", ()),
    "parent_zombie_supervisor": ("ignore_term", "die_after_ready", ()),
    "parent_reaped_supervisor": ("ignore_term", "die_after_ready", ()),
    "parent_missing_supervisor": (None, None, ()),
    "foreign_group_refusal": (None, None, ()),
    "negative_control": ("silent", None, ()),
}
TIMEOUT_CLASS = {"silent", "stderr_flood", "ignore_term", "descendant", "continuous_flood", "capture_fault"}
PARENT_RECOVERY_CASES = {
    "parent_live_supervisor", "parent_zombie_supervisor",
    "parent_reaped_supervisor", "parent_missing_supervisor",
}
WORKER_RECOVERY_CASES = {"die_before_publish", "stalled_supervisor", "stall_cleanup"}
PUBLICATION_FREE_FAULTS = {"publish_error", "die_before_publish"}
RELEASE_GATED_FIXTURES = {"clean_success", "command_failed", "early_exit_orphan", "output_flood"}
READINESS = {"descendant": ("producer.json", "child.json"), "early_exit_orphan": ("producer.json", "orphan.json")}


def deadlines(case, raw_wrapper):
    """(D_S, D_W, pytest deadline) per design section 5.1."""
    supervisor_deadline = 17.0 if raw_wrapper or case in TIMEOUT_CLASS else 8.0
    worker_deadline = supervisor_deadline + 9 + 1
    return supervisor_deadline, worker_deadline, worker_deadline + 9 + 2


# ---------------------------------------------------------------------------
# Existing oracle helpers; meanings preserved.


def guarded_group(pgid):
    if pgid <= 1 or pgid == os.getpgrp():
        raise AssertionError(f"unsafe process group: {pgid}")
    return pgid


def assert_empty(pgid, deadline=None):
    """Independent survivor oracle: pgrep counts zombies, so reap first."""
    guarded_group(pgid)
    if deadline is None:
        deadline = time.monotonic() + 2
    remaining = deadline - time.monotonic()
    if remaining < 0.05:
        raise AssertionError(f"ORACLE_SKIPPED: no time left to verify group {pgid}")
    result = subprocess.run(
        ["pgrep", "-g", str(pgid)], capture_output=True, text=True, timeout=remaining
    )
    if result.returncode == 0:
        try:
            diagnostic = subprocess.run(
                ["ps", "-o", "pid,ppid,pgid,stat,comm", "-g", str(pgid)],
                capture_output=True, text=True,
                timeout=max(0.05, deadline - time.monotonic()),
            )
            emit_diagnostic(
                f"survivor diagnostic (ps exit={diagnostic.returncode}):\n"
                f"{diagnostic.stdout}{diagnostic.stderr}"
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            emit_diagnostic(f"survivor diagnostic unavailable: {error}")
        raise AssertionError(f"survivors in group {pgid}: {result.stdout.strip()}")
    if result.returncode != 1:
        raise RuntimeError(f"pgrep failed: {result.returncode}: {result.stderr}")


# There is deliberately no unanchored kill helper in this harness. Every
# os.killpg in this file lives inside recover_descendants, immediately after
# waitpid(-G, WNOHANG) returned 0 (lemma L3); tests/test_receipt_supervisor_contracts.py
# pins that statically.


def reap_group(pgid):
    """Only reap adopted children belonging to this case's process group."""
    deadline = time.monotonic() + 2
    while True:
        try:
            pid, _ = os.waitpid(-guarded_group(pgid), os.WNOHANG)
        except ChildProcessError:
            return
        if pid:
            continue
        if time.monotonic() >= deadline:
            return
        time.sleep(0.02)


def ready(path):
    # Rename keeps readers from seeing a partially written readiness record.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp()}))
    temporary.replace(path)


def atomic_touch(path):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(f"{time.time_ns()}\n")
    temporary.replace(path)


def wait_for(directory, names, label, seconds=READINESS_SECONDS, pump=None):
    deadline = time.monotonic() + seconds
    while not all((directory / name).exists() for name in names):
        if time.monotonic() >= deadline:
            raise AssertionError(f"{label}: deadline exceeded waiting for {names}")
        if pump is not None:
            pump()
        time.sleep(TICK)


def wait_ready(directory, names, pgid, pump=None):
    wait_for(directory, names, "fixture readiness", pump=pump)
    records = {}
    for name in names:
        record = json.loads((directory / name).read_text())
        if record["pid"] <= 1 or record["pgid"] != pgid:
            raise AssertionError(f"fixture escaped wrapper group: {record}, expected {pgid}")
        emit_diagnostic(f"{name}: pid={record['pid']} pgid={record['pgid']} ready")
        records[name] = record
    return records


# ---------------------------------------------------------------------------
# Kernel facts, enumeration and the shared ancestor recovery routine.

Stat = namedtuple("Stat", "pid state ppid pgrp session")


def read_stat(pid, proc=PROC):
    try:
        raw = Path(f"{proc}/{pid}/stat").read_bytes()
    except OSError:
        return None
    tail = raw[raw.rfind(b")") + 2:].split()
    if len(tail) < 4:
        return None
    return Stat(int(pid), tail[0].decode("ascii", "replace"), int(tail[1]), int(tail[2]), int(tail[3]))


def set_subreaper(role):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), f"could not enable {role} subreaper")


def scan_children(me, until, proc=PROC):
    """Return (kids, complete, method, reason); emptiness needs complete=True.

    Both enumeration paths check the absolute ``until`` instant at the top of
    every iteration (design 5.1 absolute-bound clarification): a listing that
    cannot be finished before the deadline is returned incomplete as
    ``SCAN_DEADLINE`` and therefore never licenses an emptiness claim.
    """
    path = Path(f"{proc}/{me}/task/{me}/children")
    try:
        listed = path.read_text().split()
        method = "proc_children"
    except FileNotFoundError:
        listed = None
        method = "proc_scan"
    if listed is not None:
        kids = []
        for pid in listed:
            if time.monotonic() >= until:
                return kids, False, method, "SCAN_DEADLINE"
            stat = read_stat(pid, proc)
            if stat is None:  # a listed child cannot vanish unless we reaped it
                return kids, False, method, "CHILD_STAT_UNREADABLE"
            kids.append(stat)
        return kids, True, method, None
    for _attempt in range(3):
        kids, incomplete = [], False
        for entry in os.listdir(proc):
            if time.monotonic() >= until:
                return kids, False, method, "SCAN_DEADLINE"
            if not entry.isdigit():
                continue
            stat = read_stat(entry, proc)
            if stat is None:
                incomplete = True
                continue
            if stat.ppid == me:
                kids.append(stat)
        if not incomplete:
            return kids, True, method, None
    return kids, False, method, "SCAN_INCOMPLETE"


def reap_by_pid(pid, until):
    """waitpid(pid, WNOHANG) every 20 ms; returns the exit code or None on deadline."""
    while True:
        try:
            reaped, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            return None
        if reaped == pid:
            return os.waitstatus_to_exitcode(status)
        if time.monotonic() >= until:
            return None
        time.sleep(TICK)


def kill_pid(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def stop_adopted_children(kids, my_sid, until, record):
    """Phase 1: kill and reap by pid (lemma L4) every child that is not a member of a foreign session group.

    Checks the absolute ``until`` instant before acting on each child; returns
    False as soon as the recover partition has ended so the caller records
    RECOVERY_BUDGET_EXCEEDED instead of continuing past its bound.
    """
    for kid in kids:
        if kid.session == my_sid or kid.pgrp != kid.session:  # S, W leftovers, escapees
            if time.monotonic() >= until:
                return False
            kill_pid(kid.pid)
            record["pid_kills"].append({"pid": kid.pid, "role": "adopted", "state_at_scan": kid.state})
            reap_by_pid(kid.pid, until)
    return True


def recover_descendants(direct, trigger, expect_groups):
    """Shared ancestor recovery (design 5.3). Caller fails the case if failures is non-empty.

    Absolute bound: phase 0 is bounded by ``stop_end``; every loop in phases 1
    and 2, including the direct-children listing inside ``scan_children``, the
    per-child kill/reap loop and the per-group anchored loop, checks
    ``recover_end`` at the top of each iteration; every ``reap_by_pid`` is
    bounded by the same absolute instant; the oracle is bounded by
    ``oracle_end``. Diagnostics emitted from here on are bounded by
    ``report_end`` through the shared non-blocking writer.
    """
    me, my_sid = os.getpid(), os.getsid(0)
    stop_end, recover_end, oracle_end, report_end = trigger + 2, trigger + 6, trigger + 8, trigger + 9
    OUTPUT.deadline = report_end
    record = {
        "pid_kills": [], "groups": {}, "escape": [], "live_before_signal": 0,
        "scan": {"method": None, "complete": False, "reason": None}, "failures": [],
        "partitions": {"stop": stop_end, "recover": recover_end, "oracle": oracle_end, "report": report_end},
    }
    # phase 0: stop the direct child by pid under lemma L4; seeds the record.
    if direct is not None:
        stat = read_stat(direct)
        if stat is None:
            record["failures"].append("DIRECT_CHILD_VANISHED")
        elif stat.ppid != me:
            record["failures"].append(f"DIRECT_CHILD_NOT_OURS:{direct}:ppid={stat.ppid}")
        else:
            kill_pid(direct)
            record["pid_kills"].append({"pid": direct, "role": "direct", "state_at_scan": stat.state})
            if reap_by_pid(direct, stop_end) is None:
                record["failures"].append("DIRECT_STOP_TIMEOUT")
    # phases 1 and 2, interleaved until a complete empty scan and proof for every group.
    while time.monotonic() < recover_end:
        kids, complete, method, reason = scan_children(me, recover_end)
        record["scan"] = {"method": method, "complete": complete, "reason": reason}
        foreign = [kid for kid in kids if kid.session != my_sid]
        for kid in foreign:
            if kid.pgrp != kid.session:
                record["escape"].append(kid.pid)
            record["groups"].setdefault(
                str(kid.session), {"kills": 0, "reaped": 0, "proof": False, "live_seen": False}
            )
        if not stop_adopted_children(kids, my_sid, recover_end, record):
            continue  # partition ended: the while condition is now false and records the overrun
        progress, expired = False, False
        for group_key, group in record["groups"].items():
            if group["proof"]:
                continue
            if time.monotonic() >= recover_end:
                expired = True
                break
            group_id = int(group_key)
            if not group["kills"] and not group["live_seen"] and any(
                kid.state in "SRDT" for kid in foreign if kid.session == group_id
            ):
                group["live_seen"] = True
                record["live_before_signal"] += 1
            try:
                reaped, _ = os.waitpid(-guarded_group(group_id), os.WNOHANG)
            except ChildProcessError:
                if complete:
                    group["proof"] = True  # L5 + L5a
                continue
            if reaped > 0:
                group["reaped"] += 1
            else:
                try:
                    os.killpg(group_id, signal.SIGKILL)  # L3: immediately after waitpid(-G) == 0
                except ProcessLookupError:
                    pass
                group["kills"] += 1
            progress = True
        if expired:
            continue  # partition ended: the while condition is now false and records the overrun
        if complete and not kids and all(group["proof"] for group in record["groups"].values()):
            break
        if not progress:
            time.sleep(TICK)
    else:
        record["failures"].append("RECOVERY_BUDGET_EXCEEDED")
    if not record["scan"]["complete"]:
        record["failures"].append("SCAN_INCOMPLETE")
    # phase 3: independent oracle, timeouts equal remaining partition time.
    for group_key in record["groups"]:
        try:
            assert_empty(int(group_key), deadline=oracle_end)
        except AssertionError as error:
            record["failures"].append(f"ORACLE:{error}")
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            record["failures"].append(f"ORACLE_ERROR:{error}")
    if record["escape"] or len(record["groups"]) > max(expect_groups, 1):
        record["failures"].append("TREE_INVARIANT_VIOLATED")
    return record


# ---------------------------------------------------------------------------
# Bounded non-blocking drain and relay (design 5.4).


class Drain:
    def __init__(self, fd, retain):
        self.fd = fd
        self.retain = retain
        self.buffer = bytearray()
        self.dropped = 0
        self.eof = False

    def pump(self):
        if self.fd is None or self.eof:
            return
        total = 0
        for _ in range(DRAIN_READS_PER_TICK):
            try:
                chunk = os.read(self.fd, DRAIN_BYTES_PER_TICK - total)
            except BlockingIOError:
                return
            if not chunk:
                self.eof = True
                return
            total += len(chunk)
            room = self.retain - len(self.buffer)
            if room >= len(chunk):
                self.buffer += chunk
            else:
                if room > 0:
                    self.buffer += chunk[:room]
                self.dropped += len(chunk) - max(room, 0)
            if total >= DRAIN_BYTES_PER_TICK:
                return

    def drain_to_eof(self, deadline):
        while not self.eof and self.fd is not None:
            self.pump()
            if self.eof or time.monotonic() >= deadline:
                break
            time.sleep(0.005)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def bounded_write(fd, data, deadline, write=os.write, sleep=time.sleep, now=time.monotonic):
    """Write all of ``data`` to a non-blocking ``fd`` with at least one attempt.

    The deadline is checked only before a *retry*: an already expired budget
    still gets exactly one non-blocking write attempt (design 5.1, "the report
    is attempted once non-blocking if nothing remains"). True only if complete.
    """
    view = memoryview(data)
    attempts = 0
    while view:
        if attempts and now() >= deadline:
            return False
        attempts += 1
        try:
            written = write(fd, view)
        except BlockingIOError:
            if now() >= deadline:
                return False
            sleep(WRITE_RETRY_SECONDS)
            continue
        except OSError:
            return False
        view = view[written:]
    return True


def relay(data, deadline, fd=1):
    """Non-blocking bounded write of bytes to our stdout; False if incomplete."""
    try:
        blocking = os.get_blocking(fd)
        os.set_blocking(fd, False)
    except OSError:
        return False
    try:
        return bounded_write(fd, data, deadline)
    finally:
        try:
            os.set_blocking(fd, blocking)
        except OSError:
            pass


def clip_output(data, remaining):
    """Return (kept, dropped) with len(kept) <= max(remaining, 0), preserving line boundaries.

    A clipped diagnostic always ends with a newline: its last kept byte is
    replaced by one, so the next write, in particular the authoritative WORKER
    line, always starts a fresh line and parse_marker can still find it. With
    no room the diagnostic is dropped whole.
    """
    room = max(remaining, 0)
    if len(data) <= room:
        return data, 0
    kept = data[:room - 1] + b"\n" if room > 0 else b""
    return kept, len(data) - len(kept)


class Output:
    """Bounded non-blocking writer for a harness process's own stdout (design 5.4).

    Diagnostics share one 64 KiB budget; the report line is exempt from the
    byte budget but, like every write, is bounded by ``deadline`` (the report
    partition once a recovery has been triggered, otherwise one second).
    Nothing in W or P writes to stdout through blocking ``print``.
    """

    def __init__(self, fd=1, budget=DIAGNOSTIC_BUDGET):
        self.fd = fd
        self.remaining = budget
        self.dropped = 0
        self.incomplete = False
        self.deadline = None
        self.line_open = False

    def write(self, text, budgeted=True):
        data = text.encode("utf-8", "replace")
        if not data.endswith(b"\n"):
            data += b"\n"
        if budgeted:
            data, dropped = clip_output(data, self.remaining)
            self.remaining -= len(data)
            self.dropped += dropped
            if not data:
                return False
        if self.line_open:
            # An unfinished line on the pipe must never prefix the next record.
            data = b"\n" + data
        deadline = self.deadline if self.deadline is not None else time.monotonic() + 1.0
        complete = relay(data, deadline, self.fd)
        # Conservative: an incomplete relay may have left a partial line on the
        # pipe even though the intended buffer ended with a newline, so the line
        # stays open until a relay completes.
        self.line_open = (not complete) or not data.endswith(b"\n")
        if not complete:
            self.incomplete = True
        return complete


OUTPUT = Output()


def emit_diagnostic(text):
    return OUTPUT.write(text)


def complete_status_line(buffer):
    """Consumer rule: one newline-terminated line of fixed-key JSON, else None."""
    end = buffer.find(b"\n")
    if end < 0:
        return None
    try:
        record = json.loads(bytes(buffer[:end]).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(record, dict) or tuple(record) != STATUS_KEYS:
        return None
    return record


def hang():
    while True:
        time.sleep(3600)


# ---------------------------------------------------------------------------
# Fixtures: the pipeline members inside G.


def fixture(case, directory):
    if case in ("ignore_term", "descendant", "child", "orphan", "continuous_flood"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if case == "descendant":
        # Inherits stdout and the timeout group; deliberately holds the pipe.
        subprocess.Popen([sys.executable, str(HERE), "--fixture", "child", str(directory)])
    if case == "early_exit_orphan":
        # TERM-ignoring child that outlives its parent; does not hold the pipe.
        subprocess.Popen(
            [sys.executable, str(HERE), "--fixture", "orphan", str(directory)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        wait_for(directory, ["orphan.json"], "orphan readiness")
    if case == "clean_success":
        sys.stdout.buffer.write(FIXED_OUTPUT)
        sys.stdout.buffer.flush()
    ready(directory / {"child": "child.json", "orphan": "orphan.json"}.get(case, "producer.json"))
    if case in RELEASE_GATED_FIXTURES:
        try:
            wait_for(directory, ["release"], "release", seconds=30)
        except AssertionError:
            os._exit(99)
    if case == "clean_success" or case == "early_exit_orphan":
        os._exit(0)
    if case == "command_failed":
        sys.stdout.buffer.write(b"partial output before failure\n")
        sys.stdout.buffer.flush()
        os._exit(3)
    if case == "output_flood":
        chunk = b"f" * 65536
        try:
            for _ in range(64):  # 4 MiB
                os.write(1, chunk)
        except BrokenPipeError:
            pass
        os._exit(0)
    if case == "continuous_flood":
        chunk = b"c" * 65536
        try:
            while True:
                os.write(1, chunk)
        except BrokenPipeError:
            pass
    while True:
        if case == "stderr_flood":
            os.write(2, b"x" * 65536)
        else:
            time.sleep(60)


def inner_command(fixture_name, directory):
    producer = [sys.executable, str(HERE), "--fixture", fixture_name, str(directory)]
    reader = [sys.executable, "-c", "import shutil, sys; shutil.copyfileobj(sys.stdin.buffer, sys.stdout.buffer)"]
    pipeline = f"{shlex.join(producer)} | head -c 2097153 | {shlex.join(reader)}"
    return [
        "timeout", "-k", "2", "10", "bash", "--noprofile", "--norc",
        "-o", "pipefail", "+m", "-c", pipeline,
    ]


# ---------------------------------------------------------------------------
# Worker W.


def verify_group_identity(group, supervisor_pid):
    """Lemma L2/L7 consumer check: G is a session leader whose parent is our S."""
    guarded_group(group)
    stat = read_stat(group)
    if stat is None or not (stat.pid == stat.pgrp == stat.session == group):
        raise AssertionError(f"PUBLISHED_PGID_NOT_OWNED: {group} is not a live session leader: {stat}")
    if supervisor_pid is None or stat.ppid != supervisor_pid:
        raise AssertionError(
            f"PUBLISHED_PGID_NOT_OWNED: leader {group} has ppid {stat.ppid}, supervisor {supervisor_pid}"
        )
    return stat


def mtime_ns(path):
    return path.stat().st_mtime_ns


class Worker:
    def __init__(self, case, directory, raw_wrapper):
        self.case = case
        self.directory = directory
        self.raw_wrapper = raw_wrapper
        self.fixture, self.fault, self.options = CASES[case]
        self.supervisor = None
        self.supervisor_reaped = False
        self.drain = None
        self.prefill_fd = None
        self.group = None
        self.readiness = {}
        self.report = {"case": case, "raw_wrapper": raw_wrapper, "group": None}

    # -- helpers -----------------------------------------------------------

    def pump(self):
        if self.drain is not None:
            self.drain.pump()

    def note(self, message):
        """Bounded diagnostic: shares the 64 KiB budget, never blocks (design 5.4)."""
        OUTPUT.write(f"{self.case}: {message}")

    def emit(self):
        """The one WORKER report line, bounded by the current output deadline."""
        self.report["diagnostics"] = {"dropped": OUTPUT.dropped, "incomplete": OUTPUT.incomplete}
        OUTPUT.write("WORKER " + json.dumps(self.report, sort_keys=True), budgeted=False)

    def release_fixture(self):
        """Run the case's pre-release action, then release the fixture.

        closed_reader must close S's stdout reader BEFORE release. Releasing
        first lets S report into a still-open pipe and exit 0, which erases the
        exit 11 discriminator the case exists to prove.
        """
        if self.case == "closed_reader":
            self.drain.close()
            self.note("closed the supervisor stdout reader before release")
        atomic_touch(self.directory / "release")

    # -- entry -------------------------------------------------------------

    def run(self):
        set_subreaper("worker")
        if self.case == "negative_control" or self.raw_wrapper:
            return self.raw_path()
        if self.case == "foreign_group_refusal":
            return self.refusal_path()
        if self.case == "parent_missing_supervisor":
            atomic_touch(self.directory / "worker.stalled")
            hang()
        return self.supervised_path()

    # -- raw path: original wrapper, no supervisor -------------------------

    def raw_path(self):
        producer = [sys.executable, str(HERE), "--fixture", self.fixture, str(self.directory)]
        negative = self.case == "negative_control"
        command = producer if negative else inner_command(self.fixture, self.directory)
        started = time.monotonic()
        process = subprocess.Popen(
            command, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        pgid = process.pid
        self.report["group"] = pgid
        failure = None  # not named `error`: the except clauses below unbind that name
        try:
            guarded_group(pgid)
            # Publish immediately so the parent can clean up on its own deadline.
            (self.directory / "wrapper.pgid").write_text(str(pgid))
            names = READINESS.get(self.fixture, ("producer.json",))
            wait_ready(self.directory, names, pgid)
            if negative:
                try:
                    assert_empty(pgid)
                except AssertionError as error:
                    if "survivors in group" not in str(error):
                        raise
                else:
                    raise AssertionError("negative control was not detected")
                self.note(f"live group {pgid} correctly rejected")
            else:
                result = process.wait(timeout=max(0.01, 16 - (time.monotonic() - started)))
                elapsed = time.monotonic() - started
                self.note(f"raw wrapper exit={result} elapsed={elapsed:.3f}s group={pgid}")
                self.report.update({"wrapper_exit": result, "elapsed": round(elapsed, 3)})
                if result not in TIMEOUT_EXITS or not 9 <= elapsed <= 16:
                    raise AssertionError(f"unexpected timeout result={result}, elapsed={elapsed:.3f}s")
                reap_group(pgid)
                # Positive characterization: the raw wrapper MUST leak here.
                try:
                    assert_empty(pgid)
                except AssertionError as error:
                    if "survivors in group" not in str(error):
                        raise
                    self.note(f"raw wrapper leak detected before any cleanup: {error}")
                else:
                    raise AssertionError(
                        "raw wrapper leak was not detected; the leak control is no longer a positive characterization"
                    )
                record = recover_descendants(None, time.monotonic(), 1)
                self.report["recovery"] = record
                group = record["groups"].get(str(pgid))
                if record["pid_kills"] != []:
                    raise AssertionError(f"raw leak recovery killed by pid unexpectedly: {record}")
                if record["live_before_signal"] < 1 or group is None or group["kills"] < 1:
                    raise AssertionError(f"raw leak recovery did not find live members: {record}")
                if not record["scan"]["complete"] or record["failures"]:
                    raise AssertionError(f"raw leak recovery incomplete: {record}")
                assert_empty(pgid)
                self.note(f"group={pgid} empty after recovery")
        except BaseException as caught:  # noqa: BLE001 - recorded; anchored cleanup still runs
            failure = caught
            self.note(f"FAILURE {type(caught).__name__}: {caught}")
        # Final cleanup is ownership-anchored (lemmas L3/L4): killpg is issued
        # only right after waitpid(-G, WNOHANG) returned 0 and never after
        # ECHILD, so a group id whose reservation has lapsed is never signaled.
        # On the leak path recovery already proved G empty, so this must act on
        # nothing; on the negative control it is the explicit cleanup.
        final = recover_descendants(None, time.monotonic(), 1)
        self.report["final_cleanup"] = final
        try:
            if final["failures"]:
                raise AssertionError(f"final anchored cleanup failed: {final['failures']}")
            assert_empty(pgid)
        except BaseException as caught:  # noqa: BLE001 - do not mask the primary failure
            failure = failure or caught
        if negative and failure is None:
            self.note(f"group={pgid} empty after explicit anchored cleanup")
        self.emit()
        if failure is not None:
            raise failure

    # -- foreign group refusal ---------------------------------------------

    def refusal_path(self):
        published = int((self.directory / "wrapper.pgid").read_text())
        self.report["published"] = published
        try:
            verify_group_identity(published, None)
        except AssertionError as error:
            self.note(str(error))
            record = recover_descendants(None, time.monotonic(), 0)
            self.report["recovery"] = record
            self.emit()
            raise
        raise AssertionError(f"foreign group {published} was accepted as owned")

    # -- supervised path ---------------------------------------------------

    def spawn_supervisor(self):
        inner = inner_command(self.fixture, self.directory)
        pgid_file = str(self.directory / "wrapper.pgid")
        if self.fault:
            argv = [sys.executable, "-I", "-S", str(FAULT_RUNNER), "--fault", self.fault]
        else:
            argv = [sys.executable, "-I", "-S", str(SUPERVISOR)]
        argv += ["--pgid-file", pgid_file, *self.options, "--", *inner]
        read_end, write_end = os.pipe()
        os.set_blocking(read_end, False)
        self.started = time.monotonic()
        self.supervisor = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=write_end, stderr=subprocess.DEVNULL,
        )
        if self.case == "stalled_reader":
            # Pre-fill the pipe and never drain: the report cannot complete.
            os.set_blocking(write_end, False)
            self.prefill = 0
            try:
                while True:
                    self.prefill += os.write(write_end, b"z" * 4096)
            except BlockingIOError:
                pass
            self.prefill_fd = write_end
            self.drain = Drain(None, WORKER_RETAIN)
            self.drain.raw_fd = read_end
        else:
            os.close(write_end)
            self.drain = Drain(read_end, WORKER_RETAIN)
        self.note(f"supervisor pid={self.supervisor.pid} fault={self.fault} options={list(self.options)}")

    def acquire_identity(self):
        names = READINESS.get(self.fixture, ("producer.json",))
        directory = self.directory
        supervisor_pid = self.supervisor.pid
        gated = self.fault is not None and self.fault not in PUBLICATION_FREE_FAULTS
        if self.fault in PUBLICATION_FREE_FAULTS:
            wait_for(directory, names, "readiness", pump=self.pump)
            wait_for(directory, ["fault.fired"], "FAULT_NOT_FIRED", pump=self.pump)
            if (directory / "wrapper.pgid").exists():
                raise AssertionError("wrapper.pgid was published despite the publication fault")
            record = json.loads((directory / "producer.json").read_text())
            self.group = guarded_group(record["pgid"])
            self.readiness = {"producer.json": record}
            self.note(f"publication-free identity from readiness record: group={self.group}")
        else:
            wait_for(directory, names, "readiness", pump=self.pump)
            wait_for(directory, ["wrapper.pgid"], "publication", pump=self.pump)
            group = int((directory / "wrapper.pgid").read_text().strip())
            stat = verify_group_identity(group, supervisor_pid)
            self.readiness = wait_ready(directory, names, group, pump=self.pump)
            self.group = group
            atomic_touch(directory / "identity.ready")
            self.release_fixture()
            self.note(f"identity verified: group={group} leader={stat} supervisor={supervisor_pid}")
            if gated:
                wait_for(directory, ["fault.fired"], "FAULT_NOT_FIRED", pump=self.pump)
                if mtime_ns(directory / "fault.fired") < mtime_ns(directory / "identity.ready"):
                    raise AssertionError("fault fired before identity acknowledgment")
                self.note("fault fired after identity acknowledgment")
        self.report["group"] = self.group

    def supervised_path(self):
        self.spawn_supervisor()
        error = None
        try:
            self.supervised_body()
        except BaseException as caught:  # noqa: BLE001 - recorded, fallback still runs
            error = caught
            self.note(f"FAILURE {type(caught).__name__}: {caught}")
        fallback = None
        if self.report.get("recovery") is None:
            direct = None if self.supervisor_reaped else self.supervisor.pid
            fallback = recover_descendants(direct, time.monotonic(), 1)
            self.report["fallback"] = fallback
        self.emit()
        if error is not None:
            raise error
        if fallback is not None and (
            fallback["pid_kills"]
            or any(group["kills"] or group["reaped"] for group in fallback["groups"].values())
            or fallback["failures"]
        ):
            raise AssertionError(f"LEAK_AFTER_SUPERVISOR_EXIT: fallback cleanup acted: {fallback}")

    def supervised_body(self):
        case = self.case
        directory = self.directory
        if self.fault != "inject_signal:startup:1":
            self.acquire_identity()
        if case in PARENT_RECOVERY_CASES:
            return self.parent_case_body()
        if case == "hup_mid_run":
            os.kill(self.supervisor.pid, signal.SIGHUP)
        elif case == "term_mid_run":
            os.kill(self.supervisor.pid, signal.SIGTERM)
        # closed_reader closed its reader inside release_fixture, before release.
        supervisor_deadline = deadlines(case, False)[0]
        trigger, supervisor_exit = self.main_loop(supervisor_deadline)
        if trigger is not None:
            return self.worker_recovery(trigger)
        elapsed = time.monotonic() - self.started
        self.report.update({"supervisor_exit": supervisor_exit, "elapsed": round(elapsed, 3)})
        self.note(f"supervisor exit={supervisor_exit} elapsed={elapsed:.3f}s group={self.group}")
        status = self.collect_status()
        self.report["status"] = status
        self.assert_status(status, supervisor_exit, elapsed)
        if self.group is not None:
            # This oracle precedes any harness cleanup: cleanup cannot hide leaks.
            assert_empty(self.group, deadline=time.monotonic() + 2)
            self.note(f"exit={supervisor_exit} elapsed={elapsed:.3f}s group={self.group} empty")

    def main_loop(self, supervisor_deadline):
        """Returns (trigger, supervisor_exit); exactly one is not None."""
        pid = self.supervisor.pid
        stalled = self.directory / "supervisor.stalled"
        observe_pipe = self.case not in ("closed_reader", "stalled_reader")
        while True:
            now = time.monotonic()
            if now >= self.started + supervisor_deadline:
                return "deadline", None
            self.pump()
            if stalled.exists():
                return "supervisor.stalled", None
            if observe_pipe and self.drain.eof:
                if complete_status_line(self.drain.buffer) is None:
                    return "pipe_eof_without_status", None
                exit_code = reap_by_pid(pid, time.monotonic() + 2)
                if exit_code is None:
                    return "exit_after_status_not_observed", None
                self.supervisor_reaped = True
                return None, exit_code
            if not observe_pipe:
                try:
                    reaped, status = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    return "supervisor_not_our_child", None
                if reaped == pid:
                    self.supervisor_reaped = True
                    return None, os.waitstatus_to_exitcode(status)
            time.sleep(TICK)

    def collect_status(self):
        drain = self.drain
        if self.case == "stalled_reader":
            os.close(self.prefill_fd)
            self.prefill_fd = None
            drain.fd = drain.raw_fd
            os.set_blocking(drain.fd, False)
            drain.drain_to_eof(time.monotonic() + 1)
            data = bytes(drain.buffer)
            if data[: self.prefill] != b"z" * self.prefill:
                raise AssertionError("pre-filled pipe contents were disturbed")
            remainder = data[self.prefill:]
            if b"\n" in remainder:
                raise AssertionError(f"a status line reached a stalled reader: {remainder[:200]!r}")
            self.note(f"stalled reader received {len(remainder)} bytes after {self.prefill} pre-filled bytes")
            return None
        if self.case == "closed_reader":
            return None
        drain.drain_to_eof(time.monotonic() + 1)
        if drain.dropped:
            raise AssertionError(f"STATUS_LINE_OVERSIZE: {drain.dropped} bytes dropped")
        status = complete_status_line(drain.buffer)
        if status is None:
            raise AssertionError(f"no complete status line: {bytes(drain.buffer[:400])!r}")
        return status

    def assert_status(self, status, supervisor_exit, elapsed):
        case = self.case
        directory = self.directory
        fired = directory / "fault.fired"
        if case in ("stalled_reader", "closed_reader"):
            if supervisor_exit != 11:
                raise AssertionError(f"expected exit 11 for an unreadable report, got {supervisor_exit}")
            return
        if status["exit_code"] != supervisor_exit:
            raise AssertionError(f"status exit_code {status['exit_code']} != process exit {supervisor_exit}")
        expected = {
            "silent": "INNER_TIMEOUT", "stderr_flood": "INNER_TIMEOUT", "ignore_term": "INNER_TIMEOUT",
            "descendant": "INNER_TIMEOUT", "continuous_flood": "INNER_TIMEOUT",
            "clean_success": "OK", "command_failed": "COMMAND_FAILED", "early_exit_orphan": "ORPHANS_SWEPT",
            "output_flood": "OUTPUT_OVERFLOW", "publish_error": "SUPERVISOR_ERROR",
            "capture_fault": "SUPERVISOR_ERROR", "injected_startup": "INTERRUPTED",
            "injected_cleanup_repeat": "INTERRUPTED", "hup_mid_run": "INTERRUPTED",
            "term_mid_run": "INTERRUPTED", "outer_deadline": "OUTER_TIMEOUT",
        }[case]
        if status["status"] != expected:
            raise AssertionError(f"expected {expected}, got {status['status']}: {status}")
        if case != "injected_startup":
            if status["pgid"] != self.group:
                raise AssertionError(f"status pgid {status['pgid']} != verified group {self.group}")
            if not status["cleanup_proof"]:
                raise AssertionError(f"cleanup_proof missing: {status}")
            if status["teardowns"] != 1:
                raise AssertionError(f"teardowns != 1: {status}")
        if case in ("silent", "stderr_flood", "ignore_term", "descendant", "continuous_flood", "capture_fault"):
            if status["inner_exit"] not in TIMEOUT_EXITS or not 9 <= elapsed <= 16:
                raise AssertionError(f"unexpected timeout result={status['inner_exit']}, elapsed={elapsed:.3f}s")
        if case in ("ignore_term", "descendant", "continuous_flood", "publish_error", "capture_fault",
                    "hup_mid_run", "term_mid_run", "outer_deadline"):
            if status["kills"] < 1:
                raise AssertionError(f"expected at least one killpg: {status}")
        if case == "continuous_flood" and status["dropped"] < 1:
            raise AssertionError(f"expected dropped output: {status}")
        if case == "clean_success":
            import base64
            if status["kills"] or status["reaped"] or status["dropped"]:
                raise AssertionError(f"clean success was not clean: {status}")
            if base64.b64decode(status["output"] or "") != FIXED_OUTPUT:
                raise AssertionError(f"captured output mismatch: {status['output']!r}")
        if case == "command_failed" and (status["output"] is not None or status["kills"] or status["inner_exit"] != 3):
            raise AssertionError(f"command failure contract violated: {status}")
        if case == "early_exit_orphan" and status["inner_exit"] != 0 and not (status["reaped"] or status["kills"]):
            raise AssertionError(f"orphan sweep not recorded: {status}")
        if case == "early_exit_orphan" and (status["reaped"] + status["kills"]) < 1:
            raise AssertionError(f"orphan sweep not recorded: {status}")
        if case == "output_flood":
            if status["dropped"] < 1 or status["output"] is not None or status["inner_exit"] != 0:
                raise AssertionError(f"overflow contract violated: {status}")
        if case == "publish_error":
            if status["error"] != "publish" or not fired.exists() or (directory / "wrapper.pgid").exists():
                raise AssertionError(f"publish fault contract violated: {status}")
            if status["pgid"] != self.readiness["producer.json"]["pgid"]:
                raise AssertionError("status pgid disagrees with readiness record")
        if case == "capture_fault":
            if status["error"] != "capture" or status["dropped"] != 0 or status["output"] is not None:
                raise AssertionError(f"capture fault contract violated: {status}")
        if case == "injected_startup":
            if status["signal_phase"] != "startup" or status["teardowns"] != 0 or status["pgid"] is not None:
                raise AssertionError(f"startup interruption contract violated: {status}")
            for name in ("wrapper.pgid", "identity.ready", "producer.json"):
                if (directory / name).exists():
                    raise AssertionError(f"{name} exists although nothing was spawned")
            if not fired.exists():
                raise AssertionError("fault.fired missing for startup injection")
        if case == "injected_cleanup_repeat":
            if status["signals_received"] != 2 or status["signal_phase"] != "cleanup":
                raise AssertionError(f"repeated cleanup signals not recorded: {status}")
        if case in ("hup_mid_run", "term_mid_run"):
            wanted = "SIGHUP" if case == "hup_mid_run" else "SIGTERM"
            if status["signal_phase"] != "wait" or status["signals_received"] < 1:
                raise AssertionError(f"mid-run signal not recorded in wait phase: {status}")
            self.report["expected_signal"] = wanted
        if case == "outer_deadline" and status["inner_exit"] != -9:
            raise AssertionError(f"outer deadline should have killed the leader: {status}")

    def worker_recovery(self, trigger):
        # recover_descendants bounds OUTPUT by its report partition (TR+9), so
        # every note and the final WORKER line below go through the bounded
        # non-blocking relay; the full record travels in the WORKER line only.
        record = recover_descendants(self.supervisor.pid, time.monotonic(), 1)
        self.supervisor_reaped = True
        self.report.update({"trigger": trigger, "recovery": record})
        self.note(f"recovery after {trigger}: failures={record['failures']} scan={record['scan']}")
        case = self.case
        if case not in WORKER_RECOVERY_CASES:
            raise AssertionError(f"unexpected recovery trigger {trigger}")
        if record["failures"]:
            raise AssertionError(f"recovery failures: {record['failures']}")
        roles = [entry["role"] for entry in record["pid_kills"]]
        if roles != ["direct"]:
            raise AssertionError(f"expected exactly the direct supervisor kill: {record['pid_kills']}")
        direct_state = record["pid_kills"][0]["state_at_scan"]
        if set(record["groups"]) != {str(self.group)}:
            raise AssertionError(f"recovered groups {set(record['groups'])} != verified {{{self.group}}}")
        published = self.directory / "wrapper.pgid"
        if published.exists() and int(published.read_text().strip()) != self.group:
            raise AssertionError("published pgid disagrees with recovered group")
        for name, entry in self.readiness.items():
            if entry["pgid"] != self.group:
                raise AssertionError(f"{name} pgid disagrees with recovered group")
        group = record["groups"][str(self.group)]
        if not record["scan"]["complete"]:
            raise AssertionError("scan incomplete")
        if case == "die_before_publish":
            if trigger != "pipe_eof_without_status" or direct_state != "Z":
                raise AssertionError(f"expected EOF trigger and a zombie supervisor: {trigger} {direct_state}")
            if published.exists():
                raise AssertionError("wrapper.pgid must be absent when death precedes publication")
            if record["live_before_signal"] < 1 or group["kills"] < 1:
                raise AssertionError(f"live workload was not recovered by killpg: {record}")
        elif case == "stalled_supervisor":
            if trigger != "supervisor.stalled" or direct_state not in ("S", "R"):
                raise AssertionError(f"expected stall marker and a live supervisor: {trigger} {direct_state}")
            if record["live_before_signal"] < 1 or group["kills"] < 1:
                raise AssertionError(f"live workload was not recovered by killpg: {record}")
        elif case == "stall_cleanup":
            if trigger != "supervisor.stalled" or direct_state not in ("S", "R"):
                raise AssertionError(f"expected stall marker and a live supervisor: {trigger} {direct_state}")
            if group["reaped"] < 1:
                raise AssertionError(f"signaled-but-unreaped members were not reaped: {record}")
        self.note(f"group={self.group} empty after {case} recovery")

    def parent_case_body(self):
        case = self.case
        supervisor_pid = self.supervisor.pid
        if case == "parent_live_supervisor":
            pass
        elif case in ("parent_zombie_supervisor", "parent_reaped_supervisor"):
            deadline = time.monotonic() + READINESS_SECONDS
            while True:
                stat = read_stat(supervisor_pid)
                if stat is not None and stat.state == "Z":
                    break
                if time.monotonic() >= deadline:
                    raise AssertionError(f"supervisor did not become a zombie: {stat}")
                time.sleep(TICK)
            if case == "parent_reaped_supervisor":
                os.waitpid(supervisor_pid, 0)
                self.supervisor_reaped = True
        self.note(f"handing {case} state to the case runner")
        self.emit()
        atomic_touch(self.directory / "worker.stalled")
        hang()


def worker(case, directory, raw_wrapper):
    Worker(case, directory, raw_wrapper).run()


# ---------------------------------------------------------------------------
# Case runner P.


def case_runner(case, directory, raw_wrapper):
    set_subreaper("case runner")
    argv = [sys.executable, str(HERE), "--worker", case, str(directory)]
    if raw_wrapper:
        argv.append("--raw-wrapper")
    read_end, write_end = os.pipe()
    os.set_blocking(read_end, False)
    _, worker_deadline, _ = deadlines(case, raw_wrapper)
    started = time.monotonic()
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=write_end, stderr=write_end)
    os.close(write_end)
    drain = Drain(read_end, RUNNER_RETAIN)
    marker = directory / "worker.stalled"
    trigger, worker_exit = None, None
    while True:
        now = time.monotonic()
        if now >= started + worker_deadline:
            trigger = "deadline"
            break
        drain.pump()
        reaped, status = os.waitpid(child.pid, os.WNOHANG)
        if reaped == child.pid:
            worker_exit = os.waitstatus_to_exitcode(status)
            break
        if marker.exists():
            trigger = "worker.stalled"
            break
        time.sleep(TICK)
    summary = {"case": case, "raw_wrapper": raw_wrapper, "trigger": trigger, "worker_exit": worker_exit,
               "verdict": None, "post_exit_scan": None, "recovery": None}
    if trigger is None:
        drain.drain_to_eof(time.monotonic() + 1)
        relay_deadline = time.monotonic() + 2
        kids, complete, method, reason = scan_children(os.getpid(), time.monotonic() + 1)
        summary["post_exit_scan"] = {"complete": complete, "method": method, "reason": reason,
                                     "children": [kid._asdict() for kid in kids]}
        if not complete or kids:
            summary["recovery"] = recover_descendants(None, time.monotonic(), 1)
            summary["verdict"] = "LEAK_AFTER_WORKER_EXIT"
            exit_code = 1
        elif worker_exit == 0:
            summary["verdict"] = "WORKER_OK"
            exit_code = 0
        else:
            summary["verdict"] = f"WORKER_FAILED:{worker_exit}"
            exit_code = worker_exit if worker_exit > 0 else 1
    else:
        trigger_time = time.monotonic()
        record = recover_descendants(child.pid, trigger_time, 1)
        drain.drain_to_eof(record["partitions"]["report"])
        relay_deadline = record["partitions"]["report"]
        summary["recovery"] = record
        verdict = parent_expectation(case, directory, trigger, record)
        summary["verdict"] = verdict
        exit_code = 0 if verdict == "RECOVERED_AS_EXPECTED" else 1
    drain.close()
    payload = bytes(drain.buffer)
    if drain.dropped:
        payload += f"\n[case runner dropped {drain.dropped} bytes of worker output]\n".encode()
    if payload and not payload.endswith(b"\n"):
        payload += b"\n"
    payload += ("CASE_RUNNER " + json.dumps(summary, sort_keys=True) + "\n").encode()
    if not relay(payload, relay_deadline):
        return RELAY_INCOMPLETE_EXIT
    return exit_code


def parent_expectation(case, directory, trigger, record):
    if case not in PARENT_RECOVERY_CASES:
        return f"UNEXPECTED_RECOVERY:{trigger}"
    if trigger != "worker.stalled":
        return f"UNEXPECTED_TRIGGER:{trigger}"
    if record["failures"]:
        return "RECOVERY_FAILURES:" + ",".join(record["failures"])
    roles = [entry["role"] for entry in record["pid_kills"]]
    states = [entry["state_at_scan"] for entry in record["pid_kills"]]
    groups = record["groups"]
    total_kills = sum(group["kills"] for group in groups.values())
    if not record["scan"]["complete"]:
        return "SCAN_INCOMPLETE"
    if case == "parent_missing_supervisor":
        if roles != ["direct"] or groups or total_kills:
            return f"MISMATCH:{roles}:{groups}"
        return "RECOVERED_AS_EXPECTED"
    published = directory / "wrapper.pgid"
    readiness = directory / "producer.json"
    if not published.exists() or not readiness.exists():
        return "IDENTITY_FILES_MISSING"
    group_id = int(published.read_text().strip())
    if json.loads(readiness.read_text())["pgid"] != group_id:
        return "READINESS_PGID_DISAGREES"
    if set(groups) != {str(group_id)}:
        return f"GROUP_MISMATCH:{set(groups)}:{group_id}"
    group = groups[str(group_id)]
    if case == "parent_live_supervisor":
        if roles != ["direct", "adopted"] or states[1] not in ("S", "R"):
            return f"MISMATCH:{roles}:{states}"
        if record["live_before_signal"] < 1 or group["kills"] < 1:
            return f"NOT_LIVE_RECOVERY:{record['live_before_signal']}:{group}"
    elif case == "parent_zombie_supervisor":
        if roles != ["direct", "adopted"] or states[1] != "Z":
            return f"MISMATCH:{roles}:{states}"
        if group["kills"] < 1:
            return f"NO_KILLPG:{group}"
    elif case == "parent_reaped_supervisor":
        if roles != ["direct"]:
            return f"MISMATCH:{roles}:{states}"
        if group["kills"] < 1:
            return f"NO_KILLPG:{group}"
    return "RECOVERED_AS_EXPECTED"


# ---------------------------------------------------------------------------
# pytest / unittest layer: spawns only P, never signals a group.


def parse_marker(output, marker):
    found = None
    for line in output.splitlines():
        if line.startswith(marker + " "):
            found = json.loads(line[len(marker) + 1:])
    return found


@unittest.skipUnless(sys.platform == "linux", "requires Linux; Windows skip is not cleanup proof")
class ReceiptInventoryTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for command in ("timeout", "bash", "head", "pgrep", "ps"):
            if shutil.which(command) is None:
                raise RuntimeError(f"missing required Linux prerequisite: {command}")
        version = subprocess.run(
            ["timeout", "--version"], capture_output=True, text=True, timeout=2, check=True
        )
        if "GNU coreutils" not in version.stdout:
            raise RuntimeError("these tests require GNU coreutils timeout")
        print(version.stdout.splitlines()[0], flush=True)

    def launch(self, case, directory, raw_wrapper=False):
        _, _, pytest_deadline = deadlines(case, raw_wrapper)
        argv = [sys.executable, str(HERE), "--case", case, str(directory)]
        if raw_wrapper:
            argv.append("--raw-wrapper")
        child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            output, _ = child.communicate(timeout=pytest_deadline)
        except subprocess.TimeoutExpired:
            child.kill()
            output, _ = child.communicate(timeout=5)
            published = directory / "wrapper.pgid"
            if published.exists():
                diagnostic = subprocess.run(
                    ["pgrep", "-g", published.read_text().strip()], capture_output=True, text=True, timeout=2
                )
                output += f"\n[diagnostic pgrep -g {published.read_text().strip()}: {diagnostic.stdout.strip()}]"
            self.fail(f"{case}: CASE_RUNNER_STALLED_RECOVERY_NOT_PERFORMED after {pytest_deadline}s\n{output}")
        print(output, end="", flush=True)
        return child.returncode, output

    def run_case(self, case, raw_wrapper=False):
        with tempfile.TemporaryDirectory(prefix="receipt-timeout-") as temporary:
            directory = Path(temporary)
            code, output = self.launch(case, directory, raw_wrapper)
        self.assertEqual(code, 0, output)
        runner = parse_marker(output, "CASE_RUNNER")
        self.assertIsNotNone(runner, output)
        worker_report = parse_marker(output, "WORKER")
        return runner, worker_report

    def assert_clean_exit(self, runner):
        self.assertIsNone(runner["trigger"])
        self.assertEqual(runner["worker_exit"], 0)
        self.assertEqual(runner["verdict"], "WORKER_OK")
        self.assertTrue(runner["post_exit_scan"]["complete"])
        self.assertEqual(runner["post_exit_scan"]["children"], [])

    def assert_supervised(self, case, status_name, **checks):
        runner, report = self.run_case(case)
        self.assert_clean_exit(runner)
        status = report["status"]
        self.assertEqual(status["status"], status_name, status)
        self.assertEqual(status["exit_code"], report["supervisor_exit"])
        for key, predicate in checks.items():
            self.assertTrue(predicate(status[key]), f"{case}: {key}={status[key]!r} in {status}")
        return report

    # -- original falsifiers, assertion meanings preserved ------------------

    def test_silent_producer(self):
        report = self.assert_supervised("silent", "INNER_TIMEOUT", inner_exit=lambda v: v in TIMEOUT_EXITS,
                                        cleanup_proof=lambda v: v is True)
        self.assertTrue(9 <= report["elapsed"] <= 16, report)

    def test_term_ignoring_producer(self):
        report = self.assert_supervised("ignore_term", "INNER_TIMEOUT", inner_exit=lambda v: v in TIMEOUT_EXITS,
                                        kills=lambda v: v >= 1, cleanup_proof=lambda v: v is True)
        self.assertTrue(9 <= report["elapsed"] <= 16, report)

    def test_pipe_holding_descendant(self):
        report = self.assert_supervised("descendant", "INNER_TIMEOUT", inner_exit=lambda v: v in TIMEOUT_EXITS,
                                        kills=lambda v: v >= 1, cleanup_proof=lambda v: v is True)
        self.assertTrue(9 <= report["elapsed"] <= 16, report)

    def test_stderr_flood(self):
        report = self.assert_supervised("stderr_flood", "INNER_TIMEOUT", inner_exit=lambda v: v in TIMEOUT_EXITS,
                                        cleanup_proof=lambda v: v is True)
        self.assertTrue(9 <= report["elapsed"] <= 16, report)

    def test_survivor_detector_negative_control(self):
        runner, report = self.run_case("negative_control")
        self.assert_clean_exit(runner)
        # The explicit cleanup is ownership-anchored: it found the live producer
        # and signaled its group only after waitpid(-G) proved ownership.
        final = report["final_cleanup"]
        self.assertEqual(final["pid_kills"], [])
        self.assertGreaterEqual(final["live_before_signal"], 1)
        self.assertGreaterEqual(final["groups"][str(report["group"])]["kills"], 1)
        self.assertTrue(final["scan"]["complete"])
        self.assertEqual(final["failures"], [])

    # -- raw wrapper leak controls: positive characterization, not xfail ----

    def assert_raw_leak(self, case):
        runner, report = self.run_case(case, raw_wrapper=True)
        self.assert_clean_exit(runner)
        self.assertIn(report["wrapper_exit"], TIMEOUT_EXITS, report)
        recovery = report["recovery"]
        self.assertEqual(recovery["pid_kills"], [])
        self.assertGreaterEqual(recovery["live_before_signal"], 1)
        self.assertGreaterEqual(recovery["groups"][str(report["group"])]["kills"], 1)
        self.assertTrue(recovery["scan"]["complete"])
        self.assertEqual(recovery["failures"], [])
        # After recovery proved G empty, the final anchored cleanup must find
        # nothing to own and therefore issue no signal at all (never after ECHILD).
        final = report["final_cleanup"]
        self.assertEqual(final["pid_kills"], [])
        self.assertEqual(final["groups"], {})
        self.assertEqual(final["failures"], [])

    def test_raw_wrapper_leak_term_ignoring_producer(self):
        self.assert_raw_leak("ignore_term")

    def test_raw_wrapper_leak_pipe_holding_descendant(self):
        self.assert_raw_leak("descendant")

    # -- status contract ------------------------------------------------------

    def test_clean_early_success(self):
        report = self.assert_supervised("clean_success", "OK", exit_code=lambda v: v == 0, kills=lambda v: v == 0,
                                        reaped=lambda v: v == 0, dropped=lambda v: v == 0)
        import base64
        self.assertEqual(base64.b64decode(report["status"]["output"]), FIXED_OUTPUT)

    def test_ordinary_nonzero_exit(self):
        self.assert_supervised("command_failed", "COMMAND_FAILED", output=lambda v: v is None,
                               kills=lambda v: v == 0, inner_exit=lambda v: v == 3)

    def test_early_exit_orphan(self):
        self.assert_supervised("early_exit_orphan", "ORPHANS_SWEPT", inner_exit=lambda v: v == 0,
                               cleanup_proof=lambda v: v is True)

    def test_output_flood(self):
        self.assert_supervised("output_flood", "OUTPUT_OVERFLOW", dropped=lambda v: v >= 1,
                               output=lambda v: v is None, exit_code=lambda v: v == 3)

    def test_continuous_flood(self):
        report = self.assert_supervised("continuous_flood", "INNER_TIMEOUT", dropped=lambda v: v >= 1,
                                        kills=lambda v: v >= 1)
        self.assertTrue(9 <= report["elapsed"] <= 16, report)

    def test_stalled_reader(self):
        runner, report = self.run_case("stalled_reader")
        self.assert_clean_exit(runner)
        self.assertEqual(report["supervisor_exit"], 11, report)

    def test_closed_reader(self):
        runner, report = self.run_case("closed_reader")
        self.assert_clean_exit(runner)
        self.assertEqual(report["supervisor_exit"], 11, report)

    # -- faults ---------------------------------------------------------------

    def test_publish_error(self):
        self.assert_supervised("publish_error", "SUPERVISOR_ERROR", error=lambda v: v == "publish",
                               kills=lambda v: v >= 1, cleanup_proof=lambda v: v is True)

    def test_capture_fault(self):
        report = self.assert_supervised("capture_fault", "SUPERVISOR_ERROR", error=lambda v: v == "capture",
                                        dropped=lambda v: v == 0, output=lambda v: v is None,
                                        inner_exit=lambda v: v in TIMEOUT_EXITS, kills=lambda v: v >= 1)
        self.assertTrue(9 <= report["elapsed"] <= 16, report)

    def test_injected_startup_signal(self):
        self.assert_supervised("injected_startup", "INTERRUPTED", exit_code=lambda v: v == 8,
                               signal_phase=lambda v: v == "startup", teardowns=lambda v: v == 0,
                               pgid=lambda v: v is None)

    def test_injected_cleanup_repeat(self):
        self.assert_supervised("injected_cleanup_repeat", "INTERRUPTED", teardowns=lambda v: v == 1,
                               signals_received=lambda v: v == 2, signal_phase=lambda v: v == "cleanup")

    def test_hup_mid_run(self):
        self.assert_supervised("hup_mid_run", "INTERRUPTED", teardowns=lambda v: v == 1,
                               signal_phase=lambda v: v == "wait", kills=lambda v: v >= 1)

    def test_term_mid_run(self):
        self.assert_supervised("term_mid_run", "INTERRUPTED", teardowns=lambda v: v == 1,
                               signal_phase=lambda v: v == "wait", kills=lambda v: v >= 1)

    def test_outer_deadline(self):
        self.assert_supervised("outer_deadline", "OUTER_TIMEOUT", inner_exit=lambda v: v == -9,
                               kills=lambda v: v >= 1)

    # -- worker-level recovery -------------------------------------------------

    def assert_worker_recovery(self, case, trigger):
        runner, report = self.run_case(case)
        self.assert_clean_exit(runner)
        self.assertEqual(report["trigger"], trigger, report)
        recovery = report["recovery"]
        self.assertEqual([entry["role"] for entry in recovery["pid_kills"]], ["direct"])
        self.assertEqual(set(recovery["groups"]), {str(report["group"])})
        self.assertEqual(recovery["failures"], [])
        self.assertTrue(recovery["scan"]["complete"])
        return recovery, report

    def test_die_before_publish(self):
        recovery, report = self.assert_worker_recovery("die_before_publish", "pipe_eof_without_status")
        self.assertEqual(recovery["pid_kills"][0]["state_at_scan"], "Z")
        self.assertGreaterEqual(recovery["live_before_signal"], 1)
        self.assertGreaterEqual(recovery["groups"][str(report["group"])]["kills"], 1)

    def test_stalled_supervisor(self):
        recovery, report = self.assert_worker_recovery("stalled_supervisor", "supervisor.stalled")
        self.assertIn(recovery["pid_kills"][0]["state_at_scan"], ("S", "R"))
        self.assertGreaterEqual(recovery["live_before_signal"], 1)
        self.assertGreaterEqual(recovery["groups"][str(report["group"])]["kills"], 1)

    def test_stall_cleanup_reaps_signaled_members(self):
        recovery, report = self.assert_worker_recovery("stall_cleanup", "supervisor.stalled")
        self.assertIn(recovery["pid_kills"][0]["state_at_scan"], ("S", "R"))
        self.assertGreaterEqual(recovery["groups"][str(report["group"])]["reaped"], 1)

    # -- case-runner-level recovery ------------------------------------------

    def assert_parent_recovery(self, case, roles):
        runner, _ = self.run_case(case)
        self.assertEqual(runner["trigger"], "worker.stalled", runner)
        self.assertEqual(runner["verdict"], "RECOVERED_AS_EXPECTED", runner)
        recovery = runner["recovery"]
        self.assertEqual([entry["role"] for entry in recovery["pid_kills"]], roles)
        self.assertEqual(recovery["failures"], [])
        self.assertTrue(recovery["scan"]["complete"])
        return recovery

    def test_parent_recovers_live_supervisor(self):
        recovery = self.assert_parent_recovery("parent_live_supervisor", ["direct", "adopted"])
        self.assertIn(recovery["pid_kills"][1]["state_at_scan"], ("S", "R"))
        self.assertGreaterEqual(recovery["live_before_signal"], 1)
        self.assertEqual(len(recovery["groups"]), 1)

    def test_parent_recovers_zombie_supervisor(self):
        recovery = self.assert_parent_recovery("parent_zombie_supervisor", ["direct", "adopted"])
        self.assertEqual(recovery["pid_kills"][1]["state_at_scan"], "Z")
        self.assertEqual(len(recovery["groups"]), 1)

    def test_parent_recovers_reaped_supervisor(self):
        recovery = self.assert_parent_recovery("parent_reaped_supervisor", ["direct"])
        self.assertEqual(len(recovery["groups"]), 1)

    def test_parent_recovers_missing_supervisor(self):
        recovery = self.assert_parent_recovery("parent_missing_supervisor", ["direct"])
        self.assertEqual(recovery["groups"], {})

    # -- foreign group refusal -------------------------------------------------

    def test_foreign_group_refusal(self):
        decoy = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            with tempfile.TemporaryDirectory(prefix="receipt-timeout-") as temporary:
                directory = Path(temporary)
                (directory / "wrapper.pgid").write_text(f"{decoy.pid}\n")
                code, output = self.launch("foreign_group_refusal", directory)
            self.assertNotEqual(code, 0, output)
            self.assertIn("PUBLISHED_PGID_NOT_OWNED", output)
            runner = parse_marker(output, "CASE_RUNNER")
            self.assertIsNotNone(runner, output)
            self.assertIsNone(runner["trigger"])
            self.assertTrue(runner["post_exit_scan"]["complete"])
            self.assertEqual(runner["post_exit_scan"]["children"], [])
            report = parse_marker(output, "WORKER")
            self.assertIsNotNone(report, output)
            self.assertEqual(report["recovery"]["groups"], {})
            self.assertEqual(report["recovery"]["pid_kills"], [])
            self.assertIsNone(decoy.poll(), "decoy was signaled although it was never owned")
        finally:
            decoy.kill()
            decoy.wait(timeout=5)


if __name__ == "__main__":
    if len(sys.argv) >= 4 and sys.argv[1] in ("--case", "--worker", "--fixture"):
        raw_wrapper = "--raw-wrapper" in sys.argv[4:]
        if sys.argv[1] == "--fixture":
            fixture(sys.argv[2], Path(sys.argv[3]))
        elif sys.argv[1] == "--worker":
            worker(sys.argv[2], Path(sys.argv[3]), raw_wrapper)
        else:
            sys.exit(case_runner(sys.argv[2], Path(sys.argv[3]), raw_wrapper))
    else:
        unittest.main()
