"""Cross-platform contracts for the receipt supervisor and its Linux harness.

These run on every platform, including the Windows discovery run where the
process-tree cases skip. They are discriminators for the five 2026-09-16
implementation findings on candidate 9db77514, not cleanup proof: each test
fails against the pre-fold code and passes against the folded code.

  1. No unanchored killpg: every ``os.killpg`` in the harness lives inside
     ``recover_descendants`` and every one in the supervisor inside
     ``_cleanup_phase``; the old ``kill_group`` helper no longer exists.
  2. ``closed_reader`` closes the supervisor's stdout reader before release.
  3. Worker and case-runner output never goes through blocking ``print``; the
     bounded writer clips diagnostics and exempts the report line.
  4. An expired report budget still gets exactly one non-blocking write
     attempt, in both the supervisor and the harness relay.
  5. Enumeration and per-child recovery loops honour absolute deadlines.

Linux cleanup proof remains tests/test_receipt_inventory_timeout.py on Linux.
"""

import ast
import importlib.util
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR_PATH = ROOT / "scripts" / "receipt_inventory_supervisor.py"
HARNESS_PATH = ROOT / "tests" / "test_receipt_inventory_timeout.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


supervisor = load("receipt_supervisor_under_test", SUPERVISOR_PATH)
harness = load("receipt_harness_under_test", HARNESS_PATH)


# ---------------------------------------------------------------------------
# Static discriminators.


def enclosing_functions(tree, predicate):
    """Names of functions/methods whose body contains a node matching predicate."""
    found = set()

    def visit(node, owner):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = node.name
        if predicate(node):
            found.add(owner)
        for child in ast.iter_child_nodes(node):
            visit(child, owner)

    visit(tree, "<module>")
    return found


def is_call_to(node, module_name, attr):
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attr
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == module_name
    )


def is_print(node):
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"


class StaticAnchoringTests(unittest.TestCase):
    def setUp(self):
        self.harness_tree = ast.parse(HARNESS_PATH.read_text(encoding="utf-8"))
        self.supervisor_tree = ast.parse(SUPERVISOR_PATH.read_text(encoding="utf-8"))

    def test_harness_killpg_only_inside_recover_descendants(self):
        owners = enclosing_functions(self.harness_tree, lambda n: is_call_to(n, "os", "killpg"))
        self.assertEqual(owners, {"recover_descendants"})

    def test_harness_has_no_unanchored_kill_group_helper(self):
        names = {n.id for n in ast.walk(self.harness_tree) if isinstance(n, ast.Name)}
        defs = {n.name for n in ast.walk(self.harness_tree) if isinstance(n, ast.FunctionDef)}
        self.assertNotIn("kill_group", names | defs)

    def test_supervisor_killpg_only_inside_cleanup_phase(self):
        owners = enclosing_functions(self.supervisor_tree, lambda n: is_call_to(n, "os", "killpg"))
        self.assertEqual(owners, {"_cleanup_phase"})

    def test_worker_and_runner_never_print(self):
        # Only the pytest layer (which relays P's output) may use blocking print.
        owners = enclosing_functions(self.harness_tree, is_print)
        self.assertEqual(owners, {"launch", "setUpClass"})

    def test_supervisor_never_prints(self):
        self.assertEqual(enclosing_functions(self.supervisor_tree, is_print), set())


# ---------------------------------------------------------------------------
# Finding 4: one non-blocking attempt even when the budget has already expired.


class FakeClock:
    def __init__(self, now):
        self.now = now
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


class FakeWriter:
    def __init__(self, results):
        self.results = list(results)
        self.calls = 0

    def __call__(self, fd, view):
        self.calls += 1
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return min(result, len(view))


class BoundedWriteContract:
    bounded_write = None

    def test_expired_budget_gets_exactly_one_attempt_and_succeeds_when_complete(self):
        clock, writer = FakeClock(100.0), FakeWriter([5])
        ok = self.bounded_write(1, b"hello", deadline=50.0, write=writer, sleep=clock.sleep, now=clock.monotonic)
        self.assertTrue(ok)
        self.assertEqual(writer.calls, 1)
        self.assertEqual(clock.sleeps, [])

    def test_expired_budget_partial_write_is_incomplete_after_one_attempt(self):
        clock, writer = FakeClock(100.0), FakeWriter([2, 3])
        ok = self.bounded_write(1, b"hello", deadline=50.0, write=writer, sleep=clock.sleep, now=clock.monotonic)
        self.assertFalse(ok)
        self.assertEqual(writer.calls, 1)

    def test_expired_budget_eagain_is_incomplete_without_sleeping(self):
        clock, writer = FakeClock(100.0), FakeWriter([BlockingIOError(), 5])
        ok = self.bounded_write(1, b"hello", deadline=50.0, write=writer, sleep=clock.sleep, now=clock.monotonic)
        self.assertFalse(ok)
        self.assertEqual(writer.calls, 1)
        self.assertEqual(clock.sleeps, [])

    def test_live_budget_retries_after_eagain_until_complete(self):
        clock, writer = FakeClock(0.0), FakeWriter([BlockingIOError(), 2, 3])
        ok = self.bounded_write(1, b"hello", deadline=10.0, write=writer, sleep=clock.sleep, now=clock.monotonic)
        self.assertTrue(ok)
        self.assertEqual(writer.calls, 3)
        self.assertEqual(len(clock.sleeps), 1)

    def test_epipe_is_incomplete(self):
        clock, writer = FakeClock(0.0), FakeWriter([BrokenPipeError()])
        ok = self.bounded_write(1, b"hello", deadline=10.0, write=writer, sleep=clock.sleep, now=clock.monotonic)
        self.assertFalse(ok)


class SupervisorBoundedWriteTests(BoundedWriteContract, unittest.TestCase):
    bounded_write = staticmethod(supervisor.bounded_write)


class HarnessBoundedWriteTests(BoundedWriteContract, unittest.TestCase):
    bounded_write = staticmethod(harness.bounded_write)


# ---------------------------------------------------------------------------
# Finding 3: bounded worker output.


class OutputTests(unittest.TestCase):
    def setUp(self):
        self.relayed = []
        self.original_relay = harness.relay

        def fake_relay(data, deadline, fd=1):
            self.relayed.append((bytes(data), deadline, fd))
            return True

        harness.relay = fake_relay

    def tearDown(self):
        harness.relay = self.original_relay

    def test_clip_output_preserves_line_boundaries(self):
        # A clipped diagnostic ends with a newline; the newline byte counts against the room.
        self.assertEqual(harness.clip_output(b"abcdef\n", 4), (b"abc\n", 3))
        self.assertEqual(harness.clip_output(b"abc\n", 4), (b"abc\n", 0))
        self.assertEqual(harness.clip_output(b"abcdef\n", 1), (b"\n", 6))
        self.assertEqual(harness.clip_output(b"abc\n", 0), (b"", 4))
        self.assertEqual(harness.clip_output(b"abc\n", -5), (b"", 4))

    def test_clipped_diagnostic_never_corrupts_the_worker_line(self):
        # Ops residual reproduction (review of c72e6e55): with the old clipping the
        # stream was b"abcdWORKER {}\n" and parse_marker returned None.
        out = harness.Output(fd=7, budget=4)
        out.write("abcdef")
        self.assertTrue(out.write("WORKER {}", budgeted=False))
        stream = b"".join(data for data, _, _ in self.relayed)
        self.assertEqual(stream, b"abc\nWORKER {}\n")
        self.assertEqual(harness.parse_marker(stream.decode(), "WORKER"), {})

    def test_unfinished_line_is_terminated_before_the_next_record(self):
        out = harness.Output(fd=7, budget=64)
        out.line_open = True  # defensive path: cannot arise from write(), pinned anyway
        out.write("WORKER {}", budgeted=False)
        self.assertEqual([data for data, _, _ in self.relayed], [b"\nWORKER {}\n"])
        self.assertFalse(out.line_open)

    def test_diagnostics_share_one_budget_and_drop_beyond_it(self):
        out = harness.Output(fd=7, budget=10)
        self.assertTrue(out.write("12345"))  # 6 bytes with newline
        self.assertTrue(out.write("abcdefghij"))  # clipped to the remaining 4 bytes, newline-terminated
        self.assertEqual(out.remaining, 0)
        self.assertEqual(out.dropped, 7)
        self.assertFalse(out.write("more"))  # budget exhausted: nothing written, counted as dropped
        self.assertEqual(out.dropped, 12)
        self.assertEqual([data for data, _, _ in self.relayed], [b"12345\n", b"abc\n"])
        self.assertFalse(out.incomplete)  # clipping is not a relay failure

    def test_report_line_is_exempt_from_the_byte_budget_but_uses_the_deadline(self):
        out = harness.Output(fd=7, budget=1)
        out.deadline = 123.0
        self.assertTrue(out.write("WORKER {}", budgeted=False))
        self.assertEqual(self.relayed, [(b"WORKER {}\n", 123.0, 7)])
        self.assertEqual(out.dropped, 0)

    def test_default_deadline_is_one_second_ahead(self):
        out = harness.Output(fd=7)
        before = harness.time.monotonic()
        out.write("x")
        _, deadline, _ = self.relayed[0]
        self.assertGreaterEqual(deadline, before + 1.0)
        self.assertLess(deadline, before + 2.0)

    def test_relay_failure_marks_incomplete(self):
        harness.relay = lambda data, deadline, fd=1: False
        out = harness.Output(fd=7)
        self.assertFalse(out.write("x"))
        self.assertTrue(out.incomplete)


# ---------------------------------------------------------------------------
# Finding 2: closed_reader closes the reader before release.


class FakeDrain:
    def __init__(self, directory):
        self.directory = directory
        self.closed = False
        self.release_existed_at_close = None

    def close(self):
        self.closed = True
        self.release_existed_at_close = (self.directory / "release").exists()


class ReleaseOrderingTests(unittest.TestCase):
    def setUp(self):
        self.original_relay = harness.relay
        harness.relay = lambda data, deadline, fd=1: True
        self.temporary = harness.tempfile.TemporaryDirectory(prefix="receipt-contract-")
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        harness.relay = self.original_relay
        self.temporary.cleanup()

    def make_worker(self, case):
        worker = harness.Worker.__new__(harness.Worker)
        worker.case = case
        worker.directory = self.directory
        worker.drain = FakeDrain(self.directory)
        worker.report = {"case": case}
        return worker

    def test_closed_reader_closes_before_release(self):
        worker = self.make_worker("closed_reader")
        worker.release_fixture()
        self.assertTrue(worker.drain.closed)
        self.assertIs(worker.drain.release_existed_at_close, False)
        self.assertTrue((self.directory / "release").exists())

    def test_other_cases_release_without_closing(self):
        worker = self.make_worker("clean_success")
        worker.release_fixture()
        self.assertFalse(worker.drain.closed)
        self.assertTrue((self.directory / "release").exists())


# ---------------------------------------------------------------------------
# Finding 5: absolute deadlines in enumeration and per-child recovery.


def write_stat(proc, pid, state, ppid, pgrp, session):
    directory = proc / str(pid)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "stat").write_bytes(f"{pid} (fake proc) {state} {ppid} {pgrp} {session} 0 0\n".encode())


class ScanChildrenTests(unittest.TestCase):
    def setUp(self):
        self.temporary = harness.tempfile.TemporaryDirectory(prefix="receipt-proc-")
        self.proc = Path(self.temporary.name)
        self.me = 500

    def tearDown(self):
        self.temporary.cleanup()

    def listing(self, *pids):
        directory = self.proc / str(self.me) / "task" / str(self.me)
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "children").write_text(" ".join(str(pid) for pid in pids) + "\n")

    def test_complete_direct_listing(self):
        self.listing(501, 502)
        write_stat(self.proc, 501, "S", self.me, 501, 501)
        write_stat(self.proc, 502, "Z", self.me, 100, 100)
        kids, complete, method, reason = harness.scan_children(self.me, harness.time.monotonic() + 5, str(self.proc))
        self.assertEqual(method, "proc_children")
        self.assertTrue(complete)
        self.assertIsNone(reason)
        self.assertEqual([(k.pid, k.state, k.ppid, k.pgrp, k.session) for k in kids],
                         [(501, "S", 500, 501, 501), (502, "Z", 500, 100, 100)])

    def test_listed_child_without_stat_is_incomplete(self):
        self.listing(501, 502)
        write_stat(self.proc, 501, "S", self.me, 501, 501)
        kids, complete, _, reason = harness.scan_children(self.me, harness.time.monotonic() + 5, str(self.proc))
        self.assertFalse(complete)
        self.assertEqual(reason, "CHILD_STAT_UNREADABLE")
        self.assertEqual([k.pid for k in kids], [501])

    def test_direct_listing_honours_the_absolute_deadline(self):
        self.listing(501)
        write_stat(self.proc, 501, "S", self.me, 501, 501)
        kids, complete, method, reason = harness.scan_children(self.me, harness.time.monotonic() - 1, str(self.proc))
        self.assertEqual(method, "proc_children")
        self.assertFalse(complete)
        self.assertEqual(reason, "SCAN_DEADLINE")
        self.assertEqual(kids, [])

    def test_empty_direct_listing_is_complete_even_at_the_deadline(self):
        self.listing()
        kids, complete, _, reason = harness.scan_children(self.me, harness.time.monotonic() - 1, str(self.proc))
        self.assertTrue(complete)
        self.assertEqual(kids, [])
        self.assertIsNone(reason)

    def test_fallback_scan_finds_children_by_ppid(self):
        write_stat(self.proc, 501, "S", self.me, 501, 501)
        write_stat(self.proc, 777, "S", 1, 777, 777)
        (self.proc / "self").mkdir()
        kids, complete, method, reason = harness.scan_children(self.me, harness.time.monotonic() + 5, str(self.proc))
        self.assertEqual(method, "proc_scan")
        self.assertTrue(complete)
        self.assertIsNone(reason)
        self.assertEqual([k.pid for k in kids], [501])

    def test_fallback_scan_honours_the_absolute_deadline(self):
        write_stat(self.proc, 501, "S", self.me, 501, 501)
        _, complete, method, reason = harness.scan_children(self.me, harness.time.monotonic() - 1, str(self.proc))
        self.assertEqual(method, "proc_scan")
        self.assertFalse(complete)
        self.assertEqual(reason, "SCAN_DEADLINE")


class StopAdoptedChildrenTests(unittest.TestCase):
    def setUp(self):
        self.killed, self.reaped = [], []
        self.original = (harness.kill_pid, harness.reap_by_pid)
        harness.kill_pid = lambda pid: self.killed.append(pid)

        def reap(pid, until):
            self.reaped.append((pid, until))
            return 0

        harness.reap_by_pid = reap
        self.record = {"pid_kills": []}
        self.kids = [
            harness.Stat(11, "S", 10, 10, 1),   # our session: stopped by pid
            harness.Stat(12, "S", 10, 12, 12),  # foreign session leader in its own group: group path, untouched
            harness.Stat(13, "R", 10, 99, 12),  # escapee (pgrp != session): stopped by pid
        ]

    def tearDown(self):
        harness.kill_pid, harness.reap_by_pid = self.original

    def test_live_budget_stops_every_non_group_child_and_reaps_to_the_same_instant(self):
        until = harness.time.monotonic() + 5
        self.assertTrue(harness.stop_adopted_children(self.kids, 1, until, self.record))
        self.assertEqual(self.killed, [11, 13])
        self.assertEqual([pid for pid, _ in self.reaped], [11, 13])
        self.assertTrue(all(bound == until for _, bound in self.reaped))
        self.assertEqual([entry["pid"] for entry in self.record["pid_kills"]], [11, 13])
        self.assertEqual({entry["role"] for entry in self.record["pid_kills"]}, {"adopted"})

    def test_expired_budget_returns_false_before_acting(self):
        until = harness.time.monotonic() - 1
        self.assertFalse(harness.stop_adopted_children(self.kids, 1, until, self.record))
        self.assertEqual(self.killed, [])
        self.assertEqual(self.reaped, [])
        self.assertEqual(self.record["pid_kills"], [])


class RecoveryPartitionTests(unittest.TestCase):
    def test_recovery_sets_the_shared_output_deadline_to_the_report_partition(self):
        # recover_descendants binds OUTPUT.deadline = TR + 9 before any work;
        # the source is checked statically because the routine itself needs Linux.
        tree = ast.parse(HARNESS_PATH.read_text(encoding="utf-8"))
        routine = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "recover_descendants")
        assignments = [
            n for n in ast.walk(routine)
            if isinstance(n, ast.Assign) and len(n.targets) == 1
            and isinstance(n.targets[0], ast.Attribute) and n.targets[0].attr == "deadline"
            and isinstance(n.targets[0].value, ast.Name) and n.targets[0].value.id == "OUTPUT"
        ]
        self.assertEqual(len(assignments), 1)
        self.assertIsInstance(assignments[0].value, ast.Name)
        self.assertEqual(assignments[0].value.id, "report_end")

    def test_status_keys_match_between_supervisor_and_harness(self):
        self.assertEqual(supervisor.STATUS_KEYS, harness.STATUS_KEYS)


if __name__ == "__main__":
    unittest.main()
