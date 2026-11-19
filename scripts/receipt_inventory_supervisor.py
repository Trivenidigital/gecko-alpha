"""Receipt inventory supervisor: owns the wrapper's process group on Linux.

Stdlib only. Invoked as::

    python3 -I -S scripts/receipt_inventory_supervisor.py --pgid-file PATH \\
        [--wait 13] [--cleanup 2] [--report 0.5] -- ARGV...

ARGV is the unchanged raw wrapper (``timeout -k 2 10 bash ... -c PIPELINE``).
It is never inspected. The supervisor becomes a child subreaper, launches ARGV
in a new session so ``sid == pgid == leader pid`` (G), publishes G, waits under
an outer deadline, and on wrapper exit for any reason tears the group down
under one monotonic budget: signal, reap, verify. Exactly one bounded JSON
status line is written to stdout; stderr is never written.

Design: tasks/design_receipt_cleanup_replacement_2026_09_16.md (revision 7).
The three module-level callables ``publish``, ``read_capture`` and ``tick``
are looked up through module globals by ``main`` so the test-only fault runner
can replace them; the production script has no fault switch.
"""

import base64
import ctypes
import json
import os
import signal
import subprocess
import sys
import time

PR_SET_CHILD_SUBREAPER = 36
TIMEOUT_EXITS = (124, -9, 137)
CAPTURE_LIMIT = 65_536
CAPTURE_READS_PER_TICK = 4
LINE_LIMIT = 131_072
SURVIVOR_SCAN_CAP = 20
TICK_SECONDS = 0.02
WRITE_RETRY_SECONDS = 0.005

EXIT_CODES = {
    "OK": 0,
    "OUTPUT_OVERFLOW": 3,
    "COMMAND_FAILED": 4,
    "ORPHANS_SWEPT": 5,
    "INNER_TIMEOUT": 6,
    "OUTER_TIMEOUT": 7,
    "INTERRUPTED": 8,
    "CLEANUP_FAILED": 9,
    "SUPERVISOR_ERROR": 10,
}
REPORT_INCOMPLETE_EXIT = 11

STATUS_KEYS = (
    "status", "exit_code", "pgid", "inner_exit", "inner_elapsed", "total_elapsed",
    "reaped", "kills", "signals_received", "signal_phase", "teardowns",
    "cleanup_proof", "dropped", "overflow", "error", "survivors", "output",
)


def new_state():
    return {
        "phase": "startup",
        "signal_name": None,
        "signal_phase": None,
        "signals_received": 0,
        "pgid": None,
        "child": None,
        "inner_exit": None,
        "inner_elapsed": None,
        "reaped": 0,
        "kills": 0,
        "teardowns": 0,
        "cleanup_proof": False,
        "outer_timeout": False,
        "error": None,
        "capture_fd": None,
        "capture_enabled": False,
        "captured": bytearray(),
        "dropped": 0,
        "overflow": False,
        "survivors": None,
        "t0": None,
        "wait_end": None,
        "cleanup_end": None,
        "report_end": None,
    }


# ---------------------------------------------------------------------------
# Replaceable callables (fault runner replaces these on the module object).


def publish(path, pgid):
    """Atomically publish the inner group id: write PATH.tmp then rename."""
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="ascii") as handle:
        handle.write(f"{pgid}\n")
    os.replace(temporary, path)


def read_capture(fd, state):
    """Drain at most four reads or 65,536 bytes; never wait; never block writers."""
    total = 0
    for _ in range(CAPTURE_READS_PER_TICK):
        if time.monotonic() >= _current_partition_end(state):
            return
        try:
            chunk = os.read(fd, CAPTURE_LIMIT - total)
        except BlockingIOError:
            return
        if not chunk:
            # EOF: every writer closed. Nothing more can arrive.
            state["capture_enabled"] = False
            return
        total += len(chunk)
        room = CAPTURE_LIMIT - len(state["captured"])
        if room >= len(chunk):
            state["captured"] += chunk
        else:
            if room > 0:
                state["captured"] += chunk[:room]
            state["dropped"] += len(chunk) - max(room, 0)
        if total >= CAPTURE_LIMIT:
            return


def tick(phase, state):
    """Production pacing: 20 ms in wait and cleanup, immediate at startup."""
    if phase == "startup":
        return
    time.sleep(TICK_SECONDS)


# ---------------------------------------------------------------------------


def _current_partition_end(state):
    phase = state["phase"]
    if phase == "cleanup":
        return state["cleanup_end"]
    if phase == "report":
        return state["report_end"]
    if state["wait_end"] is None:
        return float("inf")
    return state["wait_end"]


def _install_handlers(state):
    def handler(signum, _frame):
        if state["signals_received"] == 0:
            state["signal_name"] = signal.Signals(signum).name
            state["signal_phase"] = state["phase"]
        state["signals_received"] += 1

    for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, handler)


def _set_subreaper():
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        return ctypes.get_errno()
    return 0


def _capture_step(state):
    if not state["capture_enabled"]:
        return
    try:
        read_capture(state["capture_fd"], state)
    except Exception:  # noqa: BLE001 - any capture failure disables capture only
        state["capture_enabled"] = False
        state["error"] = state["error"] or "capture"
        try:
            os.close(state["capture_fd"])
        except OSError:
            pass
        state["capture_fd"] = None


def _wait_phase(state):
    child = state["child"]
    state["phase"] = "wait"
    while True:
        if time.monotonic() >= state["wait_end"]:
            state["outer_timeout"] = True
            return
        _capture_step(state)
        if state["signals_received"]:
            return
        try:
            pid, status = os.waitpid(child, os.WNOHANG)
        except ChildProcessError:
            state["error"] = state["error"] or "wait"
            return
        except OSError:
            state["error"] = state["error"] or "wait"
            return
        if pid == child:
            state["inner_exit"] = os.waitstatus_to_exitcode(status)
            state["inner_elapsed"] = round(time.monotonic() - state["t0"], 3)
            return
        if time.monotonic() >= state["wait_end"]:
            state["outer_timeout"] = True
            return
        tick("wait", state)


def _cleanup_phase(state):
    """Anchored group loop under lemma L3: killpg only right after waitpid(-G) == 0."""
    group = state["pgid"]
    state["phase"] = "cleanup"
    state["teardowns"] = 1
    try:
        while True:
            if time.monotonic() >= state["cleanup_end"]:
                return
            _capture_step(state)
            try:
                pid, status = os.waitpid(-group, os.WNOHANG)
            except ChildProcessError:
                if state["inner_exit"] is None:
                    # ECHILD without ever reaping the leader contradicts the
                    # single-reaper invariant; name it rather than guess.
                    state["error"] = state["error"] or "wait"
                state["cleanup_proof"] = True
                return
            if pid > 0:
                if pid == group:
                    state["inner_exit"] = os.waitstatus_to_exitcode(status)
                    if state["inner_elapsed"] is None:
                        state["inner_elapsed"] = round(time.monotonic() - state["t0"], 3)
                else:
                    state["reaped"] += 1
                continue
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
            state["kills"] += 1
            tick("cleanup", state)
    except Exception:  # noqa: BLE001 - never re-entered; report names the step
        state["error"] = state["error"] or "cleanup"


def _survivor_scan(state):
    """Diagnostic only: entries in session G, capped, aborted at the deadline."""
    found = []
    group = state["pgid"]
    try:
        entries = os.listdir("/proc")
    except OSError:
        return found
    for entry in entries:
        if time.monotonic() >= state["report_end"] or len(found) >= SURVIVOR_SCAN_CAP:
            break
        if not entry.isdigit():
            continue
        fields = read_stat(int(entry))
        if fields is not None and fields[3] == group:
            found.append({"pid": int(entry), "state": fields[0], "ppid": fields[1]})
    return found


def read_stat(pid):
    """Return (state, ppid, pgrp, session) from /proc/<pid>/stat or None."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    tail = raw[raw.rfind(b")") + 2:].split()
    if len(tail) < 4:
        return None
    return (tail[0].decode("ascii", "replace"), int(tail[1]), int(tail[2]), int(tail[3]))


def _status(state):
    if not state["cleanup_proof"]:
        return "CLEANUP_FAILED"
    if state["error"]:
        return "SUPERVISOR_ERROR"
    if state["signals_received"]:
        return "INTERRUPTED"
    if state["outer_timeout"]:
        return "OUTER_TIMEOUT"
    inner = state["inner_exit"]
    if inner in TIMEOUT_EXITS:
        return "INNER_TIMEOUT"
    swept = state["reaped"] or state["kills"]
    if inner == 0 and swept:
        return "ORPHANS_SWEPT"
    if inner != 0:
        return "COMMAND_FAILED"
    if state["dropped"] or state["overflow"]:
        return "OUTPUT_OVERFLOW"
    return "OK"


def _serialize(state, status):
    record = {
        "status": status,
        "exit_code": EXIT_CODES[status],
        "pgid": state["pgid"],
        "inner_exit": state["inner_exit"],
        "inner_elapsed": state["inner_elapsed"],
        "total_elapsed": round(time.monotonic() - state["t0"], 3) if state["t0"] else None,
        "reaped": state["reaped"],
        "kills": state["kills"],
        "signals_received": state["signals_received"],
        "signal_phase": state["signal_phase"],
        "teardowns": state["teardowns"],
        "cleanup_proof": state["cleanup_proof"],
        "dropped": state["dropped"],
        "overflow": state["overflow"],
        "error": state["error"],
        "survivors": state["survivors"],
        "output": None,
    }
    if status == "OK" and state["dropped"] == 0:
        record["output"] = base64.b64encode(bytes(state["captured"])).decode("ascii")
    line = json.dumps(record, separators=(",", ":")) + "\n"
    if len(line) > LINE_LIMIT:
        record["output"] = None
        record["overflow"] = True
        state["overflow"] = True
        if status == "OK":
            record["status"] = "OUTPUT_OVERFLOW"
            record["exit_code"] = EXIT_CODES["OUTPUT_OVERFLOW"]
        line = json.dumps(record, separators=(",", ":")) + "\n"
    return line.encode("utf-8"), record["exit_code"]


def bounded_write(fd, data, deadline, write=os.write, sleep=time.sleep, now=time.monotonic):
    """Write all of ``data`` to a non-blocking ``fd`` with at least one attempt.

    The deadline is checked only before a *retry*. An already expired budget
    still gets exactly one non-blocking write attempt (design section 4:
    "attempted once non-blocking if nothing remains"); it is never skipped.
    Returns True only when every byte was written.
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


def _write_report(line, deadline):
    """Non-blocking bounded write of the one status line; False if incomplete."""
    fd = 1
    try:
        flags = os.get_blocking(fd)
        os.set_blocking(fd, False)
    except OSError:
        return False
    try:
        return bounded_write(fd, line, deadline)
    finally:
        try:
            os.set_blocking(fd, flags)
        except OSError:
            pass


def _report_phase(state):
    state["phase"] = "report"
    status = _status(state)
    if status == "CLEANUP_FAILED" and state["pgid"] is not None:
        state["survivors"] = _survivor_scan(state)
    line, exit_code = _serialize(state, status)
    if not _write_report(line, state["report_end"]):
        return REPORT_INCOMPLETE_EXIT
    return exit_code


def _parse_args(argv):
    options = {"pgid_file": None, "wait": 13.0, "cleanup": 2.0, "report": 0.5}
    index = 0
    while index < len(argv):
        item = argv[index]
        if item == "--":
            return options, argv[index + 1:]
        if item == "--pgid-file" and index + 1 < len(argv):
            options["pgid_file"] = argv[index + 1]
        elif item in ("--wait", "--cleanup", "--report") and index + 1 < len(argv):
            options[item[2:]] = float(argv[index + 1])
        else:
            return None, None
        index += 2
    return None, None


def main(argv):
    state = new_state()
    _install_handlers(state)
    options, command = _parse_args(argv)
    if options is None or not options["pgid_file"] or not command:
        state["error"] = "usage"
        state["cleanup_proof"] = True
        state["t0"] = time.monotonic()
        state["report_end"] = state["t0"] + 0.5
        return _report_phase(state)
    error = _set_subreaper()
    if error:
        state["error"] = "prctl"
        state["cleanup_proof"] = True
        state["t0"] = time.monotonic()
        state["report_end"] = state["t0"] + options["report"]
        return _report_phase(state)
    # Startup rendezvous: the only pre-spawn point a fault runner can perturb.
    tick("startup", state)
    if state["signals_received"]:
        state["cleanup_proof"] = True
        state["t0"] = time.monotonic()
        state["report_end"] = state["t0"] + options["report"]
        return _report_phase(state)

    read_end, write_end = os.pipe()
    os.set_blocking(read_end, False)
    state["capture_fd"] = read_end
    state["capture_enabled"] = True
    try:
        # Single reaper: Popen.poll/wait/communicate are never called on this
        # object; every reap below is an explicit waitpid by pid or by -G.
        # Popen has no timeout parameter. The bound on this child is the
        # absolute wait partition (t0 + --wait) enforced by _wait_phase, then
        # the cleanup partition; tests/test_round8_subprocess_timeouts.py
        # checks that no Popen result here is waited on without a timeout.
        leader = subprocess.Popen(  # noqa: S603 - exact wrapper argv, never inspected
            command, start_new_session=True, stdin=subprocess.DEVNULL,
            stdout=write_end, stderr=subprocess.DEVNULL,
        )
    except Exception:  # noqa: BLE001 - spawn failure is a supervisor error
        os.close(write_end)
        os.close(read_end)
        state["capture_fd"] = None
        state["capture_enabled"] = False
        state["error"] = "spawn"
        state["cleanup_proof"] = True
        state["t0"] = time.monotonic()
        state["report_end"] = state["t0"] + options["report"]
        return _report_phase(state)
    os.close(write_end)
    child = leader.pid
    state["t0"] = time.monotonic()
    state["wait_end"] = state["t0"] + options["wait"]
    state["cleanup_end"] = state["wait_end"] + options["cleanup"]
    state["report_end"] = state["cleanup_end"] + options["report"]
    state["child"] = child
    state["pgid"] = child  # setsid in the child: sid == pgid == pid (lemma L7)
    try:
        try:
            publish(options["pgid_file"], child)
        except Exception:  # noqa: BLE001 - recorded; teardown still runs
            state["error"] = "publish"
        else:
            _wait_phase(state)
    finally:
        _cleanup_phase(state)
        if state["capture_fd"] is not None:
            try:
                os.close(state["capture_fd"])
            except OSError:
                pass
            state["capture_fd"] = None
    return _report_phase(state)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
