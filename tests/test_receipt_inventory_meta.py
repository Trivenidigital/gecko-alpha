"""Validation tests for scripts/receipt_inventory_meta.py, synthetic and local.

Design: tasks/design_receipt_reducer_meta_2026_09_16.md (revision 2). Every
filesystem case uses a temporary root; no host path, unit name or production
value appears. Windows runs are discovery checks: link and FIFO cases skip on
Windows and are mandatory on Linux CI. Shims replace the module's ``os`` name
with a proxy so the open-time guards are reached under controlled replacement
without touching the real ``os`` module.
"""

import ast
import hashlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CANONICAL_META_PATH = ROOT / "scripts" / "receipt_inventory_meta.py"
# Mutation self-check (review fold, candidate 406ed010): a child unittest run may
# point this module at a temporary mutated copy of the script so the named
# discriminators are exercised against the mutant. The production script has
# no such switch; only this test module reads the variable.
META_SOURCE_OVERRIDE = "RECEIPT_META_SOURCE"
META_SELF_CHECK_NESTED = "RECEIPT_META_SELF_CHECK_NESTED"
META_PATH = Path(os.environ.get(META_SOURCE_OVERRIDE) or CANONICAL_META_PATH)


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


meta = load("receipt_meta_under_test", META_PATH)
real_os = os

# Literal pins.
INPUT_CAP = 4_096
FILE_CAP = 16 * 1024 * 1024
CHUNK = 65_536
READ_ITERATIONS = 4 * (FILE_CAP // CHUNK) + 2
SLOTS = ("f0", "f1", "f2", "f3", "f4", "f5", "f6", "f7")
SLOT_STATUSES = ("OK", "UNUSED", "MISSING", "LINK", "NOT_REGULAR", "TOO_LARGE", "CHANGED", "UNREADABLE")
ACTIVE_STATES = frozenset({"active", "reloading", "inactive", "failed", "activating", "deactivating", "maintenance"})
STANDARD_OUTPUTS = frozenset({"inherit", "null", "tty", "journal", "kmsg", "journal+console", "kmsg+console", "socket"})
FULL_KEYS = frozenset({
    "status", "format_ok", "head_ok", "active_state_ok", "standard_output_ok",
    "head", "active_state", "standard_output", "files",
})
HEAD = "0123456789abcdef0123456789abcdef01234567"


def metadata(head=HEAD, active="active", output="journal", swap=False):
    lines = [head, "ActiveState=" + active, "StandardOutput=" + output]
    if swap:
        lines = [lines[0], lines[2], lines[1]]
    return ("\n".join(lines) + "\n").encode("ascii")


def run_main(argv, data=None):
    out = io.BytesIO()
    code = meta.main(list(argv), stdin=io.BytesIO(metadata() if data is None else data), stdout=out)
    return code, out.getvalue()


def parse_line(line):
    assert line.endswith(b"\n") and line.count(b"\n") == 1, line
    return json.loads(line.decode("utf-8"))


def sha(data):
    return hashlib.sha256(data).hexdigest()


def fake_stat(st, **changes):
    fake = types.SimpleNamespace(
        st_mode=st.st_mode, st_ino=st.st_ino, st_dev=st.st_dev,
        st_size=st.st_size, st_mtime_ns=st.st_mtime_ns,
    )
    for name, value in changes.items():
        setattr(fake, name, value)
    return fake


class PathProxy:
    def __init__(self, overrides):
        self._overrides = overrides

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(real_os.path, name)


class OsProxy:
    """The module under test looks every ``os`` attribute up at call time, so
    replacing ``meta.os`` with this proxy shims only the named functions."""

    def __init__(self, path_overrides=None, **overrides):
        self._overrides = overrides
        self.path = PathProxy(path_overrides or {})

    def __getattr__(self, name):
        if name in self._overrides:
            return self._overrides[name]
        return getattr(real_os, name)


class Recorder:
    def __init__(self, target):
        self.target = target
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append(args)
        return self.target(*args, **kwargs)


class FstatSequence:
    """Apply one transform per fstat call in order; later calls are real."""

    def __init__(self, *transforms):
        self.transforms = list(transforms)
        self.calls = 0

    def __call__(self, fd):
        st = real_os.fstat(fd)
        index = self.calls
        self.calls += 1
        if index < len(self.transforms) and self.transforms[index] is not None:
            return self.transforms[index](st)
        return st


class TempRootCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="receipt-meta-")
        self.root = real_os.path.realpath(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        self.original_os = meta.os
        self.addCleanup(self.restore_os)

    def restore_os(self):
        meta.os = self.original_os

    def write(self, rel, data):
        path = Path(self.root, *rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def sized(self, rel, size):
        path = Path(self.root, *rel.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as handle:
            handle.truncate(size)
        return path

    def slot(self, rel, **proxy):
        if proxy:
            meta.os = OsProxy(**proxy)
        try:
            return meta.hash_slot(self.root, rel)
        finally:
            meta.os = self.original_os

    def run_files(self, *rels, data=None):
        code, line = run_main([self.root, *rels], data)
        self.assertEqual(code, 0)
        return parse_line(line)


class HappyPathTests(TempRootCase):
    def test_two_small_files_ok(self):
        a = b"alpha\n"
        b = bytes(range(256)) * 3
        self.write("a.txt", a)
        self.write("sub/b.bin", b)
        result = self.run_files("a.txt", "sub/b.bin")
        self.assertEqual(result["status"], "OK")
        self.assertTrue(all(result[flag] for flag in ("format_ok", "head_ok", "active_state_ok", "standard_output_ok")))
        self.assertEqual(result["head"], HEAD)
        self.assertEqual(result["active_state"], "active")
        self.assertEqual(result["standard_output"], "journal")
        self.assertEqual(result["files"]["f0"], {"status": "OK", "sha256": sha(a), "size": len(a)})
        self.assertEqual(result["files"]["f1"], {"status": "OK", "sha256": sha(b), "size": len(b)})
        for name in SLOTS[2:]:
            self.assertEqual(result["files"][name], {"status": "UNUSED", "sha256": None, "size": None})
        swapped = self.run_files("a.txt", data=metadata(swap=True))
        self.assertEqual(swapped["status"], "OK")


class MetadataTests(TempRootCase):
    def test_head_forms(self):
        self.write("a.txt", b"x")
        for head in (HEAD.upper(), HEAD[:39], HEAD + "0", "g" * 40, "z" + HEAD[1:]):
            code, line = run_main([self.root, "a.txt"], metadata(head=head))
            result = parse_line(line)
            self.assertEqual(result["status"], "FAILED", head)
            self.assertTrue(result["format_ok"])
            self.assertFalse(result["head_ok"])
            self.assertIsNone(result["head"])
            self.assertNotIn(head.encode("ascii"), line)
            self.assertEqual(result["files"]["f0"]["status"], "OK")

    def test_state_values(self):
        self.write("a.txt", b"x")
        for active in ACTIVE_STATES:
            self.assertEqual(self.run_files("a.txt", data=metadata(active=active))["active_state"], active)
        for output in STANDARD_OUTPUTS:
            self.assertEqual(self.run_files("a.txt", data=metadata(output=output))["standard_output"], output)
        for active, output in (("running", "journal"), ("active", "file:/x"), ("Active", "journal"), ("active", "JOURNAL")):
            code, line = run_main([self.root, "a.txt"], metadata(active=active, output=output))
            result = parse_line(line)
            self.assertEqual(result["status"], "FAILED")
            self.assertTrue(result["format_ok"])
            if active not in ACTIVE_STATES:
                self.assertFalse(result["active_state_ok"])
                self.assertIsNone(result["active_state"])
                self.assertNotIn(active.encode(), line)
            if output not in STANDARD_OUTPUTS:
                self.assertFalse(result["standard_output_ok"])
                self.assertIsNone(result["standard_output"])
                self.assertNotIn(output.encode(), line)

    def test_format_forms(self):
        self.write("a.txt", b"x")
        base = metadata()
        forms = {
            "missing_line": b"\n".join(base.split(b"\n")[:2]) + b"\n",
            "extra_line": base + b"Extra=1\n",
            "duplicate_key": HEAD.encode() + b"\nActiveState=active\nActiveState=active\n",
            "unknown_key": HEAD.encode() + b"\nActiveState=active\nSubState=running\n",
            "no_trailing_lf": base[:-1],
            "non_ascii": base.replace(b"active", "actiwé".encode("utf-8"), 1),
            "no_equals": HEAD.encode() + b"\nActiveState=active\nStandardOutput\n",
            "value_with_equals": HEAD.encode() + b"\nActiveState=active\nStandardOutput=journal=x\n",
            "empty_value": HEAD.encode() + b"\nActiveState=\nStandardOutput=journal\n",
            "empty_input": b"",
        }
        for name, data in forms.items():
            code, line = run_main([self.root, "a.txt"], data)
            result = parse_line(line)
            self.assertEqual(result["status"], "FAILED", name)
            self.assertFalse(result["format_ok"], name)
            for flag in ("head_ok", "active_state_ok", "standard_output_ok"):
                self.assertFalse(result[flag], name)
            for field in ("head", "active_state", "standard_output"):
                self.assertIsNone(result[field], name)
            self.assertNotIn(b"running", line)
            self.assertNotIn(b"Extra", line)
        # CRLF: CR is a legal VALUE byte under the grammar, so the shape is
        # accepted, but the head is not 40-hex and neither value is a set
        # member. The run is FAILED with every value null and nothing echoed.
        code, line = run_main([self.root, "a.txt"], base.replace(b"\n", b"\r\n"))
        result = parse_line(line)
        self.assertEqual(result["status"], "FAILED")
        self.assertTrue(result["format_ok"])
        for flag in ("head_ok", "active_state_ok", "standard_output_ok"):
            self.assertFalse(result[flag])
        for field in ("head", "active_state", "standard_output"):
            self.assertIsNone(result[field])
        self.assertNotIn(b"\r", line)

    def test_input_cap(self):
        self.write("a.txt", b"x")
        exact = metadata() + b"z" * (INPUT_CAP - len(metadata()))
        self.assertEqual(len(exact), INPUT_CAP)
        result = self.run_files("a.txt", data=exact)
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("files", result)

        lstat = Recorder(real_os.lstat)
        opener = Recorder(real_os.open)
        meta.os = OsProxy(lstat=lstat, open=opener)
        code, line = run_main([self.root, "a.txt"], exact + b"z")
        self.assertEqual(code, 0)
        self.assertEqual(line, b'{"status":"INPUT_CAP"}\n')
        self.assertEqual(lstat.calls, [])
        self.assertEqual(opener.calls, [])
        self.assertEqual(meta.INPUT_CAP, INPUT_CAP)


class ArgvTests(TempRootCase):
    def test_usage_forms(self):
        class Untouchable(io.BytesIO):
            def read(self, *args):
                raise AssertionError("stdin must not be read on USAGE")

        self.write("a.txt", b"x")
        bad = [
            [], [self.root], [self.root] + ["f%d" % i for i in range(9)],
            [self.root, "/a.txt"], [self.root, "../a.txt"], [self.root, "./a.txt"],
            [self.root, "sub//a.txt"], [self.root, "sub\\a.txt"], [self.root, "C:a.txt"],
            [self.root, "a" * 257], [self.root, "a" * 256], [self.root, "a" * 250 + "/" + "b" * 6],
            [self.root, "a.txt", "a.txt"], [self.root, "a\x00b"],
            [self.root, ""], [self.root, "a.txt/"], [self.root, "a b.txt"], [self.root, "."],
            ["relative/root", "a.txt"], ["", "a.txt"], ["x" * 1025, "a.txt"],
            [self.root + "\x00", "a.txt"],
        ]
        for argv in bad:
            out = io.BytesIO()
            code = meta.main(argv, stdin=Untouchable(b"data"), stdout=out)
            self.assertEqual(code, 0, argv)
            self.assertEqual(out.getvalue(), b'{"status":"USAGE"}\n', argv)
        # Segment bound is 255; total bound is 256, so 255 + "/" + 0 is impossible
        # and the longest accepted forms are a 255-char segment or 254 + "/" + 1.
        self.assertEqual(meta.parse_args([self.root, "a" * 255]), (self.root, ["a" * 255]))
        self.assertEqual(meta.parse_args([self.root, "a" * 254 + "/b"]), (self.root, ["a" * 254 + "/b"]))
        self.assertEqual(meta.parse_args([self.root, "x.y-z_1/w"]), (self.root, ["x.y-z_1/w"]))
        self.assertEqual(meta.PATH_BOUND, 256)
        self.assertEqual(meta.ROOT_BOUND, 1_024)

    def test_root_invalid(self):
        missing = real_os.path.join(self.root, "nope")
        self.assertEqual(run_main([missing, "a.txt"])[1], b'{"status":"ROOT_INVALID"}\n')
        regular = self.write("file.txt", b"x")
        self.assertEqual(run_main([str(regular), "a.txt"])[1], b'{"status":"ROOT_INVALID"}\n')


class SlotTests(TempRootCase):
    def test_missing_empty_and_oversized(self):
        self.assertEqual(self.slot("missing.txt"), {"status": "MISSING", "sha256": None, "size": None})
        self.write("empty.txt", b"")
        self.assertEqual(self.slot("empty.txt"), {"status": "OK", "sha256": sha(b""), "size": 0})
        self.sized("big.bin", FILE_CAP + 1)
        self.assertEqual(self.slot("big.bin"), {"status": "TOO_LARGE", "sha256": None, "size": None})
        self.sized("exact.bin", FILE_CAP)
        digest = hashlib.sha256()
        for _ in range(FILE_CAP // CHUNK):
            digest.update(b"\0" * CHUNK)
        self.assertEqual(self.slot("exact.bin"), {"status": "OK", "sha256": digest.hexdigest(), "size": FILE_CAP})
        self.assertEqual(meta.FILE_CAP, FILE_CAP)

    def test_read_never_exceeds_cap_plus_one(self):
        path = self.sized("grow.bin", FILE_CAP)
        requests = []
        returned = []

        def read(fd, length):
            total_before = sum(returned)
            requests.append((length, total_before))
            chunk = real_os.read(fd, length)
            returned.append(len(chunk))
            return chunk

        def append_after_first_fstat(st):
            with open(path, "ab") as handle:
                handle.write(b"g" * CHUNK)
            return fake_stat(st)

        result = self.slot("grow.bin", read=read, fstat=FstatSequence(append_after_first_fstat))
        self.assertEqual(result, {"status": "TOO_LARGE", "sha256": None, "size": None})
        self.assertEqual(sum(returned), FILE_CAP + 1)
        for length, total_before in requests:
            self.assertLessEqual(length, min(CHUNK, FILE_CAP + 1 - total_before))
            self.assertGreater(length, 0)
        self.assertEqual(meta.CHUNK, CHUNK)

    def test_canary_file_not_read(self):
        canary = b"CANARY_FILE_CONTENT_5e1f"
        canary_path = str(self.write("canary.txt", canary))
        self.write("a.txt", b"listed")
        opener = Recorder(real_os.open)
        lstat = Recorder(real_os.lstat)
        realpath = Recorder(real_os.path.realpath)
        meta.os = OsProxy({"realpath": realpath}, open=opener, lstat=lstat)
        code, line = run_main([self.root, "a.txt"])
        self.assertEqual(parse_line(line)["status"], "OK")
        self.assertNotIn(canary, line)
        self.assertNotIn(sha(canary).encode(), line)
        for recorder in (opener, lstat, realpath):
            self.assertTrue(recorder.calls)
            for args in recorder.calls:
                self.assertNotEqual(real_os.path.normcase(str(args[0])), real_os.path.normcase(canary_path))

    def make_symlink(self, source, link):
        try:
            real_os.symlink(source, link, target_is_directory=real_os.path.isdir(source))
        except (OSError, NotImplementedError) as exc:
            if sys.platform == "linux":
                raise
            self.skipTest("symlink creation unavailable on this platform: %r; Linux CI is mandatory" % (exc,))

    def test_leaf_symlink_is_link_before_open(self):
        inside = self.write("target.txt", b"inside")
        outside_dir = tempfile.TemporaryDirectory(prefix="receipt-meta-outside-")
        self.addCleanup(outside_dir.cleanup)
        outside = Path(outside_dir.name, "secret.txt")
        outside.write_bytes(b"OUTSIDE_SECRET_7c2a")
        self.make_symlink(str(inside), real_os.path.join(self.root, "link_in.txt"))
        self.make_symlink(str(outside), real_os.path.join(self.root, "link_out.txt"))
        opener = Recorder(real_os.open)
        for rel in ("link_in.txt", "link_out.txt"):
            self.assertEqual(self.slot(rel, open=opener), {"status": "LINK", "sha256": None, "size": None})
        self.assertEqual(opener.calls, [])
        code, line = run_main([self.root, "link_out.txt"])
        self.assertNotIn(b"OUTSIDE_SECRET", line)
        self.assertNotIn(sha(b"OUTSIDE_SECRET_7c2a").encode(), line)

    def test_directory_symlink_component_is_link(self):
        self.write("realdir/file.txt", b"x")
        self.make_symlink(real_os.path.join(self.root, "realdir"), real_os.path.join(self.root, "linkdir"))
        opener = Recorder(real_os.open)
        self.assertEqual(self.slot("linkdir/file.txt", open=opener), {"status": "LINK", "sha256": None, "size": None})
        self.assertEqual(opener.calls, [])
        self.assertEqual(self.slot("realdir/file.txt")["status"], "OK")

    def test_directory_is_not_regular(self):
        self.write("dir/inner.txt", b"x")
        self.assertEqual(self.slot("dir"), {"status": "NOT_REGULAR", "sha256": None, "size": None})


DRIVER = '''
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location("meta_driver", sys.argv[1])
meta = importlib.util.module_from_spec(spec)
spec.loader.exec_module(meta)
root, rel, sibling = sys.argv[2], sys.argv[3], sys.argv[4]
real = os


class PathProxy:
    def __getattr__(self, name):
        return getattr(real.path, name)

    @staticmethod
    def realpath(path, strict=False):
        return path


class Proxy:
    path = PathProxy()

    def __getattr__(self, name):
        return getattr(real, name)

    @staticmethod
    def lstat(path):
        return real.stat(sibling)


meta.os = Proxy()
sys.stdout.write(json.dumps(meta.hash_slot(root, rel)) + "\\n")
'''


@unittest.skipUnless(sys.platform == "linux", "open-time guards need O_NOFOLLOW and mkfifo; Windows skip is disclosed")
class OpenTimeGuardTests(TempRootCase):
    def test_o_nofollow_rejects_leaf_symlink_when_pre_checks_pass(self):
        target = self.write("target.txt", b"followed")
        link = real_os.path.join(self.root, "leaf.txt")
        real_os.symlink(str(target), link)
        opener = Recorder(real_os.open)
        result = self.slot(
            "leaf.txt",
            path_overrides={"realpath": lambda path, strict=False: path},
            lstat=lambda path: real_os.stat(str(target)),
            open=opener,
        )
        self.assertEqual(result, {"status": "LINK", "sha256": None, "size": None})
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(opener.calls[0][1] & real_os.O_NOFOLLOW, real_os.O_NOFOLLOW)

    def run_fifo_driver(self):
        sibling = self.write("sibling.txt", b"regular")
        fifo = real_os.path.join(self.root, "pipe.fifo")
        real_os.mkfifo(fifo)
        driver = Path(self.root, "driver.py")
        driver.write_text(DRIVER, encoding="utf-8")
        argv = [sys.executable, "-I", "-S", str(driver), str(META_PATH), self.root, "pipe.fifo", str(sibling)]
        child = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
            self.fail("FIFO_OPEN_BLOCKED: os.open did not return; O_NONBLOCK guard missing")
        self.assertEqual(child.returncode, 0, err)
        return json.loads(out.decode("utf-8"))

    def test_o_nonblock_returns_on_fifo_when_pre_checks_pass(self):
        result = self.run_fifo_driver()
        self.assertEqual(result["status"], "NOT_REGULAR")
        self.assertIsNone(result["sha256"])

    def test_post_open_fstat_rejects_nonregular(self):
        self.assertEqual(self.run_fifo_driver(), {"status": "NOT_REGULAR", "sha256": None, "size": None})


class ObservedChangeTests(TempRootCase):
    def test_byte_count_mismatch_is_changed(self):
        self.write("a.txt", b"12345")
        state = {"extra": False}

        def read(fd, length):
            chunk = real_os.read(fd, length)
            if not chunk and not state["extra"]:
                state["extra"] = True
                return b"!"
            return chunk

        self.assertEqual(self.slot("a.txt", read=read), {"status": "CHANGED", "sha256": None, "size": None})

    def test_identity_mismatch_is_changed(self):
        self.write("a.txt", b"12345")
        shim = FstatSequence(lambda st: fake_stat(st, st_ino=st.st_ino + 1))
        self.assertEqual(self.slot("a.txt", fstat=shim), {"status": "CHANGED", "sha256": None, "size": None})
        self.assertEqual(shim.calls, 1)

    def test_size_or_mtime_drift_is_changed(self):
        self.write("a.txt", b"12345")
        size_shim = FstatSequence(None, lambda st: fake_stat(st, st_size=st.st_size + 1))
        self.assertEqual(self.slot("a.txt", fstat=size_shim), {"status": "CHANGED", "sha256": None, "size": None})
        self.assertEqual(size_shim.calls, 2)
        mtime_shim = FstatSequence(None, lambda st: fake_stat(st, st_mtime_ns=st.st_mtime_ns + 1))
        self.assertEqual(self.slot("a.txt", fstat=mtime_shim), {"status": "CHANGED", "sha256": None, "size": None})
        self.assertEqual(self.slot("a.txt", fstat=FstatSequence(None, None))["status"], "OK")

    def test_growth_past_cap_is_too_large(self):
        path = self.sized("cap.bin", FILE_CAP)

        def append_one(st):
            with open(path, "ab") as handle:
                handle.write(b"!")
            return fake_stat(st)

        self.assertEqual(
            self.slot("cap.bin", fstat=FstatSequence(append_one)),
            {"status": "TOO_LARGE", "sha256": None, "size": None},
        )

    def test_read_iteration_cap(self):
        self.write("slow.bin", b"s" * (READ_ITERATIONS + 100))
        reads = []

        def one_byte(fd, length):
            reads.append(length)
            return real_os.read(fd, 1)

        self.assertEqual(self.slot("slow.bin", read=one_byte), {"status": "UNREADABLE", "sha256": None, "size": None})
        self.assertEqual(len(reads), READ_ITERATIONS)
        self.assertEqual(meta.READ_ITERATIONS, READ_ITERATIONS)


class SchemaTests(TempRootCase):
    def test_full_and_singleton_schemas_exact(self):
        self.write("a.txt", b"x")
        result = self.run_files("a.txt")
        self.assertEqual(set(result), FULL_KEYS)
        self.assertEqual(meta.FULL_KEYS, FULL_KEYS)
        self.assertEqual(tuple(result["files"]), SLOTS)
        self.assertEqual(meta.SLOTS, SLOTS)
        self.assertEqual(meta.SLOT_STATUSES, SLOT_STATUSES)
        self.assertEqual(meta.ACTIVE_STATES, ACTIVE_STATES)
        self.assertEqual(meta.STANDARD_OUTPUTS, STANDARD_OUTPUTS)
        for slot in result["files"].values():
            self.assertEqual(set(slot), {"status", "sha256", "size"})
        for flag in ("format_ok", "head_ok", "active_state_ok", "standard_output_ok"):
            self.assertIs(type(result[flag]), bool)
        failed = self.run_files("a.txt", "missing.txt")
        self.assertEqual(failed["status"], "FAILED")
        self.assertEqual(set(failed), FULL_KEYS)
        self.assertEqual(failed["files"]["f1"]["status"], "MISSING")
        self.assertEqual(failed["files"]["f0"]["status"], "OK")
        for status, argv, data in (
            ("USAGE", [], None),
            ("ROOT_INVALID", [real_os.path.join(self.root, "nope"), "a.txt"], None),
            ("INPUT_CAP", [self.root, "a.txt"], b"z" * (INPUT_CAP + 1)),
        ):
            self.assertEqual(run_main(argv, data)[1], b'{"status":"%s"}\n' % status.encode())

    def test_line_bound_constant_and_worst_case(self):
        self.assertEqual(meta.LINE_BOUND, 4096)
        worst = {
            "status": "FAILED", "format_ok": True, "head_ok": True, "active_state_ok": True,
            "standard_output_ok": True, "head": "f" * 40,
            "active_state": max(ACTIVE_STATES, key=len), "standard_output": max(STANDARD_OUTPUTS, key=len),
            "files": {name: {"status": "NOT_REGULAR", "sha256": "f" * 64, "size": FILE_CAP} for name in SLOTS},
        }
        line = meta.render(worst)
        self.assertLessEqual(len(line), meta.LINE_BOUND)
        self.assertLessEqual(len(line), 4096)
        self.assertTrue(line.endswith(b"\n") and line.count(b"\n") == 1)
        with self.assertRaises(Exception):
            meta.render({"status": "OK", "pad": "p" * 5000})
        original = meta.check
        meta.check = lambda root, rels, data: {"status": "OK", "pad": "p" * 5000}
        try:
            self.write("a.txt", b"x")
            self.assertEqual(run_main([self.root, "a.txt"])[1], b'{"status":"INTERNAL_ERROR"}\n')
        finally:
            meta.check = original

    def test_forced_exception_is_singleton_internal_error(self):
        self.write("a.txt", b"x")
        original = meta.hashlib

        def raiser():
            raise RuntimeError("forced")

        meta.hashlib = types.SimpleNamespace(sha256=raiser)
        try:
            code, line = run_main([self.root, "a.txt"])
            self.assertEqual(code, 0)
            self.assertEqual(line, b'{"status":"INTERNAL_ERROR"}\n')

            def interrupt():
                raise KeyboardInterrupt()

            meta.hashlib = types.SimpleNamespace(sha256=interrupt)
            with self.assertRaises(KeyboardInterrupt):
                run_main([self.root, "a.txt"])
        finally:
            meta.hashlib = original

    def test_exit_code_policy(self):
        class Broken(io.BytesIO):
            def write(self, data):
                raise OSError("pipe closed")

        self.write("a.txt", b"x")
        self.assertEqual(meta.main([self.root, "a.txt"], stdin=io.BytesIO(metadata()), stdout=Broken()), 1)
        self.assertEqual(run_main([self.root, "a.txt"])[0], 0)
        self.assertEqual(run_main([])[0], 0)


# ---------------------------------------------------------------------------
# Mutation self-check: the four open-time and containment guards can only be
# discriminated on Linux, so the proof that their tests kill the mutants runs
# inside this module on Linux CI rather than in a manual checklist. Each case
# copies the script source with exactly one guard removed into a temporary
# directory, then runs the named discriminating test in a bounded child
# unittest process against that copy. The verdict must be an assertion
# failure: an import error, a skip, an unexpected error or an outer timeout all
# fail the self-check. A control run against the unmodified source must pass.

MUTANTS = {
    "realpath_comparison_removed": (
        '    if real != full:\n        return _slot("LINK")\n',
        "",
        "SlotTests.test_directory_symlink_component_is_link",
        "linux",
    ),
    "o_nofollow_dropped": (
        '    | getattr(os, "O_NOFOLLOW", 0)\n',
        "",
        "OpenTimeGuardTests.test_o_nofollow_rejects_leaf_symlink_when_pre_checks_pass",
        "linux",
    ),
    "o_nonblock_dropped": (
        '    | getattr(os, "O_NONBLOCK", 0)\n',
        "",
        "OpenTimeGuardTests.test_o_nonblock_returns_on_fifo_when_pre_checks_pass",
        "linux",
    ),
    "post_open_s_isreg_removed": (
        '    if not stat.S_ISREG(st.st_mode):\n        return _slot("NOT_REGULAR")\n    if st.st_size > FILE_CAP:',
        "    if st.st_size > FILE_CAP:",
        "OpenTimeGuardTests.test_post_open_fstat_rejects_nonregular",
        "linux",
    ),
    # Cross-platform control of the mechanism itself: proves on Windows that the
    # child run, the source override and the verdict parsing work.
    "sentinel_removed": (
        "    limit = FILE_CAP + 1\n",
        "    limit = FILE_CAP\n",
        "ObservedChangeTests.test_growth_past_cap_is_too_large",
        "any",
    ),
}
SELF_CHECK_TIMEOUT = 60.0


@unittest.skipIf(os.environ.get(META_SELF_CHECK_NESTED), "nested self-check child never recurses")
class MutationSelfCheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="receipt-meta-mutant-")
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def mutant_source(self, name):
        old, new, _, _ = MUTANTS[name]
        source = CANONICAL_META_PATH.read_text(encoding="utf-8")
        self.assertEqual(source.count(old), 1, "mutation site must exist exactly once: %s" % name)
        path = self.directory / ("mutant_%s.py" % name)
        path.write_bytes(source.replace(old, new).encode("utf-8"))
        return path

    def run_child(self, source_path, test_name):
        env = dict(os.environ)
        env[META_SOURCE_OVERRIDE] = str(source_path)
        env[META_SELF_CHECK_NESTED] = "1"
        argv = [sys.executable, "-m", "unittest", "-v", "test_receipt_inventory_meta.%s" % test_name]
        posix = sys.platform != "win32"
        child = subprocess.Popen(
            argv, cwd=str(ROOT / "tests"), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=posix,
        )
        try:
            out, _ = child.communicate(timeout=SELF_CHECK_TIMEOUT)
        except subprocess.TimeoutExpired:
            if posix:
                try:
                    real_os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            child.kill()
            child.communicate(timeout=5)
            self.fail("SELF_CHECK_OUTER_TIMEOUT: %s did not finish within %ss" % (test_name, SELF_CHECK_TIMEOUT))
        text = out.decode("utf-8", "replace")
        if posix:
            self.assert_no_descendants(child.pid)
        return child.returncode, text

    def assert_no_descendants(self, session):
        """With start_new_session the child's pid is its session and group id."""
        survivors = subprocess.run(["pgrep", "-g", str(session)], capture_output=True, text=True, timeout=5)
        self.assertEqual(survivors.returncode, 1, "leaked descendants in group %s: %r" % (session, survivors.stdout))

    def assert_control_passes(self, name):
        _, _, test_name, _ = MUTANTS[name]
        code, text = self.run_child(CANONICAL_META_PATH, test_name)
        self.assertEqual(code, 0, text)
        self.assertIn("Ran 1 test", text)
        self.assertIn("\nOK", text)
        self.assertNotIn("skipped", text)

    def assert_mutant_killed(self, name, expected_fragments):
        """The child must report exactly one assertion failure whose message
        names the expected status and the status the mutant produced."""
        _, _, test_name, platform = MUTANTS[name]
        if platform == "linux" and sys.platform != "linux":
            self.skipTest("Linux-only discriminator; Windows skip is disclosed")
        self.assert_control_passes(name)
        code, text = self.run_child(self.mutant_source(name), test_name)
        self.assertEqual(code, 1, text)
        self.assertIn("Ran 1 test", text)
        self.assertIn("FAILED (failures=1)", text)
        self.assertIn("AssertionError", text)
        self.assertNotIn("errors=", text)
        self.assertNotIn("skipped", text)
        self.assertNotIn("ImportError", text)
        self.assertNotIn("ModuleNotFoundError", text)
        self.assertNotIn("SELF_CHECK_OUTER_TIMEOUT", text)
        for fragment in expected_fragments:
            self.assertIn(fragment, text)

    def test_mechanism_control_sentinel_removed(self):
        # Mutant stops reading at the cap, so the grown file is reported CHANGED
        # by the post-read fstat instead of TOO_LARGE by the sentinel byte.
        self.assert_mutant_killed("sentinel_removed", ("'status': 'TOO_LARGE'", "'status': 'CHANGED'"))

    def test_kill_realpath_comparison_removed(self):
        # Mutant hashes through the symlinked directory component: OK, not LINK.
        self.assert_mutant_killed("realpath_comparison_removed", ("'status': 'LINK'", "'status': 'OK'"))

    def test_kill_o_nofollow_dropped(self):
        # Mutant follows the leaf symlink at open time and hashes the target: OK, not LINK.
        self.assert_mutant_killed("o_nofollow_dropped", ("'status': 'LINK'", "'status': 'OK'"))

    def test_kill_o_nonblock_dropped(self):
        # Mutant blocks in os.open on the writer-less FIFO; the inner test kills
        # and reaps its driver after its own 5 s budget and fails with a fixed message.
        self.assert_mutant_killed("o_nonblock_dropped", ("FIFO_OPEN_BLOCKED",))

    def test_kill_post_open_s_isreg_removed(self):
        # Mutant proceeds past the FIFO fstat; the identity compare against the
        # shimmed regular-file lstat then reports CHANGED, not NOT_REGULAR.
        self.assert_mutant_killed("post_open_s_isreg_removed", ("'status': 'NOT_REGULAR'", "'status': 'CHANGED'"))


FORBIDDEN_NAMES = {"open", "print", "eval", "exec", "compile", "__import__", "input", "breakpoint"}
WRITE_FLAGS = {"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"}
OS_ATTRS = {"open", "read", "close", "fstat", "lstat", "path"}
OS_PATH_ATTRS = {"join", "isabs", "realpath", "isdir"}
PRODUCTION_LITERALS = (
    "journalctl", "systemctl", "gecko-pipeline", ".service", "/root/", "2026-09-14", "20:45", "21:45",
)


class StaticContractTests(unittest.TestCase):
    def setUp(self):
        self.raw = META_PATH.read_bytes()
        self.source = self.raw.decode("utf-8")
        self.tree = ast.parse(self.source)

    def test_import_allowlist(self):
        modules = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module)
        self.assertEqual(modules, {"errno", "hashlib", "json", "os", "re", "stat", "sys"})

    def test_os_attribute_allowlist(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "os":
                self.assertTrue(node.attr in OS_ATTRS or node.attr.startswith("O_"), node.attr)
            if (
                isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
                and isinstance(node.value.value, ast.Name) and node.value.value.id == "os"
                and node.value.attr == "path"
            ):
                self.assertIn(node.attr, OS_PATH_ATTRS)

    def test_no_builtin_open_and_no_write_flags(self):
        names = {n.id for n in ast.walk(self.tree) if isinstance(n, ast.Name)}
        self.assertEqual(names & FORBIDDEN_NAMES, set())
        attrs = {n.attr for n in ast.walk(self.tree) if isinstance(n, ast.Attribute)}
        self.assertEqual((names | attrs) & WRITE_FLAGS, set())
        constants = {n.value for n in ast.walk(self.tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        self.assertEqual(constants & WRITE_FLAGS, set())
        self.assertEqual(attrs & {"system", "popen", "Popen", "run", "fork", "spawn", "environ", "listdir", "walk", "remove", "unlink", "rename", "replace"}, set())

    def test_no_production_literals(self):
        for literal in PRODUCTION_LITERALS + ("/root/gecko-alpha", "gecko-pipeline.service"):
            self.assertNotIn(literal, self.source, literal)

    def test_main_guard_and_lf(self):
        self.assertNotIn(b"\r", self.raw)
        guards = [
            n for n in self.tree.body
            if isinstance(n, ast.If) and isinstance(n.test, ast.Compare)
            and isinstance(n.test.left, ast.Name) and n.test.left.id == "__name__"
        ]
        self.assertEqual(len(guards), 1)
        self.assertIn("main(sys.argv[1:])", ast.unparse(guards[0]))

    def test_isolated_interpreter_no_args_is_usage(self):
        completed = subprocess.run(
            [sys.executable, "-I", "-S", str(META_PATH)], input=b"", capture_output=True, timeout=60,
        )
        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b'{"status":"USAGE"}\n')
        self.assertNotIn(b"\r", completed.stdout)


if __name__ == "__main__":
    unittest.main()
