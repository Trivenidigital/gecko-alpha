"""Synthetic Linux proof of the receipt wrapper, never production collection.

Each case owns a subprocess/subreaper so pytest's process state is untouched.
Windows skips are discovery checks only, not evidence of Linux cleanup.
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


HERE = Path(__file__).resolve()


def guarded_group(pgid):
    if pgid <= 1 or pgid == os.getpgrp():
        raise AssertionError(f"unsafe process group: {pgid}")
    return pgid


def assert_empty(pgid):
    guarded_group(pgid)
    result = subprocess.run(
        ["pgrep", "-g", str(pgid)], capture_output=True, text=True, timeout=2
    )
    if result.returncode == 0:
        try:
            diagnostic = subprocess.run(
                ["ps", "-o", "pid,ppid,pgid,stat,comm", "-g", str(pgid)],
                capture_output=True, text=True, timeout=2,
            )
            print(
                f"survivor diagnostic (ps exit={diagnostic.returncode}):\n"
                f"{diagnostic.stdout}{diagnostic.stderr}", flush=True,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            print(f"survivor diagnostic unavailable: {error}", flush=True)
        raise AssertionError(f"survivors in group {pgid}: {result.stdout.strip()}")
    if result.returncode != 1:
        raise RuntimeError(f"pgrep failed: {result.returncode}: {result.stderr}")


def kill_group(pgid):
    guarded_group(pgid)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass


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


def fixture(case, directory):
    if case in ("ignore_term", "descendant", "child"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if case == "descendant":
        # Inherits stdout and the timeout group; deliberately holds the pipe.
        subprocess.Popen([sys.executable, str(HERE), "--fixture", "child", str(directory)])
    ready(directory / ("child.json" if case == "child" else "producer.json"))
    while True:
        if case == "stderr_flood":
            os.write(2, b"x" * 65536)
        else:
            time.sleep(60)


def wait_ready(directory, names, pgid):
    deadline = time.monotonic() + 4
    while not all((directory / name).exists() for name in names):
        if time.monotonic() >= deadline:
            raise AssertionError(f"fixture readiness deadline exceeded: {names}")
        time.sleep(0.02)
    for name in names:
        record = json.loads((directory / name).read_text())
        if record["pid"] <= 1 or record["pgid"] != pgid:
            raise AssertionError(f"fixture escaped wrapper group: {record}, expected {pgid}")
        print(f"{name}: pid={record['pid']} pgid={record['pgid']} ready", flush=True)


def worker(case, directory):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
        raise OSError(ctypes.get_errno(), "could not enable worker subreaper")
    producer = [sys.executable, str(HERE), "--fixture", case, str(directory)]
    negative = case == "negative_control"
    if negative:
        command = producer
    else:
        reader = [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"]
        pipeline = f"{shlex.join(producer)} | head -c 2097153 | {shlex.join(reader)}"
        command = [
            "timeout", "-k", "2", "10", "bash", "--noprofile", "--norc",
            "-o", "pipefail", "+m", "-c", pipeline,
        ]
    started = time.monotonic()
    process = subprocess.Popen(
        command, start_new_session=True, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    pgid = process.pid
    try:
        guarded_group(pgid)
        # Publish immediately so the parent can clean up on its own deadline.
        (directory / "wrapper.pgid").write_text(str(pgid))
        names = ["producer.json", "child.json"] if case == "descendant" else ["producer.json"]
        wait_ready(directory, names, pgid)
        if negative:
            try:
                assert_empty(pgid)
            except AssertionError as error:
                if "survivors in group" not in str(error):
                    raise
            else:
                raise AssertionError("negative control was not detected")
            print(f"{case}: live group {pgid} correctly rejected", flush=True)
        else:
            result = process.wait(timeout=max(0.01, 16 - (time.monotonic() - started)))
            elapsed = time.monotonic() - started
            print(f"{case}: wrapper exit={result} elapsed={elapsed:.3f}s group={pgid}", flush=True)
            if result not in (124, -9, 137) or not 9 <= elapsed <= 16:
                raise AssertionError(f"unexpected timeout result={result}, elapsed={elapsed:.3f}s")
            reap_group(pgid)
            # This assertion precedes fallback cleanup: cleanup cannot hide leaks.
            assert_empty(pgid)
            print(f"{case}: exit={result} elapsed={elapsed:.3f}s group={pgid} empty", flush=True)
    finally:
        kill_group(pgid)
        process.wait(timeout=2)
        reap_group(pgid)
        assert_empty(pgid)
    if negative:
        print(f"{case}: group={pgid} empty after explicit cleanup", flush=True)


@unittest.skipUnless(sys.platform == "linux", "requires Linux; Windows skip is not cleanup proof")
class ReceiptInventoryTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for command in ("timeout", "bash", "head", "pgrep"):
            if shutil.which(command) is None:
                raise RuntimeError(f"missing required Linux prerequisite: {command}")
        version = subprocess.run(
            ["timeout", "--version"], capture_output=True, text=True, timeout=2, check=True
        )
        if "GNU coreutils" not in version.stdout:
            raise RuntimeError("these tests require GNU coreutils timeout")
        print(version.stdout.splitlines()[0], flush=True)

    def run_case(self, case):
        with tempfile.TemporaryDirectory(prefix="receipt-timeout-") as temporary:
            directory = Path(temporary)
            child = subprocess.Popen(
                [sys.executable, str(HERE), "--worker", case, temporary],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            try:
                output, _ = child.communicate(timeout=25)
            except subprocess.TimeoutExpired:
                # Kill wrapper group first. Give the living subreaper time to reap
                # its adopted descendants before stopping the worker itself.
                record = directory / "wrapper.pgid"
                if record.exists():
                    kill_group(int(record.read_text()))
                try:
                    output, _ = child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    output, _ = child.communicate(timeout=2)
                self.fail(f"{case}: worker exceeded 25s deadline\n{output}")
            print(output, end="", flush=True)
            self.assertEqual(child.returncode, 0, output)

    def test_silent_producer(self):
        self.run_case("silent")

    def test_term_ignoring_producer(self):
        self.run_case("ignore_term")

    def test_pipe_holding_descendant(self):
        self.run_case("descendant")

    def test_stderr_flood(self):
        self.run_case("stderr_flood")

    def test_survivor_detector_negative_control(self):
        self.run_case("negative_control")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] in ("--worker", "--fixture"):
        action = worker if sys.argv[1] == "--worker" else fixture
        action(sys.argv[2], Path(sys.argv[3]))
    else:
        unittest.main()
