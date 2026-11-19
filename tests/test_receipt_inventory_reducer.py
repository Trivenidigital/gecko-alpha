"""Adversarial, cross-platform tests for scripts/receipt_inventory_reducer.py.

Design: tasks/design_receipt_reducer_meta_2026_09_16.md (revision 2). Every
test names the guard it discriminates; a guard whose removal keeps this module
green is a comment, not a guard. Synthetic input only: no journal, no host, no
production window. Runs in-process through ``main(argv, stdin, stdout)`` and
through ``python -I -S`` subprocesses with bounded timeouts.
"""

import ast
import importlib.util
import io
import json
import random
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REDUCER_PATH = ROOT / "scripts" / "receipt_inventory_reducer.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


reducer = load("receipt_reducer_under_test", REDUCER_PATH)

# Literal pins: the oracle must not move when the script drifts.
EVENTS = (
    "ledger_coverage_check_failed",
    "ledger_emission_recorded",
    "ledger_label_pass",
    "ledger_price_lookup_failed",
    "ledger_record_failed",
    "ledger_record_rollback_failed",
    "ledger_record_skipped_db_closed",
    "trade_decision_event_emit_failed",
    "trade_decision_event_emitted",
    "trade_decision_event_rollback_failed",
    "trade_decision_event_skipped_db_closed",
    "trade_decision_events_pruned",
    "volume_history_cg_pruned",
)
KEYS = (
    "decision", "event_id", "kind", "ledger_id", "level", "reason",
    "signal_type", "site", "surface", "timestamp", "token_id",
)
COUNTERS = (
    "malformed", "duplicate_key", "ts_invalid", "out_of_window",
    "message_nonstr", "message_nonjson", "event_invalid",
)
OK_KEYS = frozenset(
    ("status", "bytes", "records", "saturated", "observed_span", "events", "keys",
     "unknown_events", "unknown_keys") + COUNTERS
)
BYTE_CAP = 2_097_152
RECORD_CAP = 200
LINE_BOUND = 4_096
MAX_END_US = 253_402_300_799_999_999

# Synthetic window: one hour of epoch microseconds, no production value.
START = 1_600_000_000_000_000
END = START + 3_600_000_000
ARGV = [str(START), str(END)]
TS = str(START + 1_000_000)


def message(event, **fields):
    body = {"event": event}
    body.update(fields)
    return json.dumps(body)


def record(ts=TS, msg=None, raw=None, extra=None, omit_ts=False):
    """One journald-shaped record. ``msg`` is a MESSAGE string; ``raw`` is any
    JSON value placed verbatim in MESSAGE (list, number, null, object)."""
    body = {}
    if not omit_ts:
        body["__REALTIME_TIMESTAMP"] = ts
    if raw is not None:
        body["MESSAGE"] = raw
    elif msg is not None:
        body["MESSAGE"] = msg
    if extra:
        body.update(extra)
    return json.dumps(body).encode("utf-8") + b"\n"


def good(event=EVENTS[0], ts=TS, **fields):
    return record(ts=ts, msg=message(event, **fields))


def run_main(data, argv=ARGV):
    out = io.BytesIO()
    code = reducer.main(list(argv), stdin=io.BytesIO(data), stdout=out)
    return code, out.getvalue()


def run_subprocess(data, argv=ARGV, timeout=60):
    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(REDUCER_PATH), *argv],
        input=data, capture_output=True, timeout=timeout,
    )
    return completed.returncode, completed.stdout


def parse_line(line):
    assert line.endswith(b"\n") and line.count(b"\n") == 1, line
    return json.loads(line.decode("utf-8"))


def reduce_ok(data, start=START, end=END):
    result = reducer.reduce(data, start, end)
    assert result["status"] == "OK", result
    return result


def conservation_holds(result):
    counted = sum(result[name] for name in COUNTERS)
    events = sum(result["events"].values())
    return (
        result["records"] == counted + events + result["unknown_events"]
        and all(v <= events for v in result["keys"].values())
        and result["unknown_keys"] <= events
    )


class CountingLoads:
    def __init__(self):
        self.calls = []
        self.original = reducer._loads

    def __call__(self, text):
        self.calls.append(text)
        return self.original(text)


class LeakageTests(unittest.TestCase):
    CANARIES = (
        "CANARY_9f3a", "https://example.invalid/leak?x=1",
        "Traceback (most recent call last): ValueError: boom",
        "sk-live-0123456789abcdef", "0xDEADBEEFCAFEBABE",
    )

    def test_canaries_never_appear(self):
        data = b"".join([
            good(ledger_id=self.CANARIES[0], reason=self.CANARIES[1]),            # allowlisted keys
            good(foreign=self.CANARIES[2]),                                       # unknown key
            record(msg=message(self.CANARIES[3])),                                # unknown event
            record(msg="not json " + self.CANARIES[4]),                           # non-JSON MESSAGE
            record(msg=message(EVENTS[1]), extra={"_HOSTNAME": self.CANARIES[0]}),  # outer field
            record(ts=self.CANARIES[3], msg=message(EVENTS[2])),                  # bad timestamp
            record(raw=[67, 65, 78, 65, 82, 89]),                                 # list form
        ])
        _, line = run_main(data)
        for canary in self.CANARIES:
            self.assertNotIn(canary.encode("utf-8"), line)
        self.assertNotIn(b"foreign", line)
        self.assertNotIn(b"_HOSTNAME", line)
        result = parse_line(line)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["records"], 7)


class ParsingTests(unittest.TestCase):
    def test_duplicate_outer_key_counted_and_not_parsed(self):
        data = (b'{"__REALTIME_TIMESTAMP":"' + TS.encode() + b'","MESSAGE":"' +
                json.dumps(message(EVENTS[0])).encode()[1:-1] +
                b'","__REALTIME_TIMESTAMP":"1"}\n')
        result = reduce_ok(data)
        self.assertEqual(result["duplicate_key"], 1)
        self.assertEqual(sum(result["events"].values()), 0)
        self.assertEqual(result["ts_invalid"], 0)

    def test_duplicate_message_key_counted(self):
        inner = '{"event":"%s","event":"%s"}' % (EVENTS[0], EVENTS[1])
        result = reduce_ok(record(msg=inner))
        self.assertEqual(result["duplicate_key"], 1)
        self.assertEqual(sum(result["events"].values()), 0)
        self.assertEqual(result["message_nonjson"], 0)

    def test_message_list_number_null_object_are_nonstr(self):
        list_form = [ord(c) for c in message(EVENTS[0])]
        data = b"".join([
            record(raw=list_form),
            record(raw=42),
            b'{"__REALTIME_TIMESTAMP":"%s","MESSAGE":null}\n' % TS.encode(),
            record(raw={"event": EVENTS[0]}),
            record(),  # MESSAGE absent
        ])
        result = reduce_ok(data)
        self.assertEqual(result["message_nonstr"], 5)
        self.assertEqual(sum(result["events"].values()), 0)

    def test_message_nonjson_and_nondict(self):
        data = b"".join([
            record(msg="plain text"),
            record(msg='["%s"]' % EVENTS[0]),
            record(msg='"%s"' % EVENTS[0]),
            record(msg="12"),
        ])
        result = reduce_ok(data)
        self.assertEqual(result["message_nonjson"], 4)
        self.assertEqual(sum(result["events"].values()), 0)

    def test_event_missing_nonstr_unknown(self):
        data = b"".join([
            record(msg=json.dumps({"ledger_id": "x"})),
            record(msg=json.dumps({"event": 7})),
            record(msg=json.dumps({"event": None})),
            record(msg=json.dumps({"event": "no_such_event", "ledger_id": "x", "foreign": 1})),
        ])
        result = reduce_ok(data)
        self.assertEqual(result["event_invalid"], 3)
        self.assertEqual(result["unknown_events"], 1)
        # Keys of an unknown-event record are not inspected.
        self.assertEqual(result["keys"]["ledger_id"], 0)
        self.assertEqual(result["unknown_keys"], 0)

    def test_event_only_message_has_no_unknown_keys(self):
        result = reduce_ok(good())
        self.assertEqual(result["events"][EVENTS[0]], 1)
        self.assertEqual(result["unknown_keys"], 0)
        self.assertEqual(set(result["keys"].values()), {0})

    def test_known_keys_only_has_no_unknown_keys(self):
        result = reduce_ok(good(ledger_id="a", site="b", level="info"))
        self.assertEqual(result["unknown_keys"], 0)
        self.assertEqual(result["keys"]["ledger_id"], 1)
        self.assertEqual(result["keys"]["site"], 1)
        self.assertEqual(result["keys"]["level"], 1)
        self.assertEqual(result["keys"]["reason"], 0)

    def test_one_unknown_key_counts_once(self):
        every = {name: "v" for name in KEYS}
        result = reduce_ok(good(foreign=1, **every))
        self.assertEqual(result["unknown_keys"], 1)
        self.assertEqual(set(result["keys"].values()), {1})
        result = reduce_ok(good(foreign=1, other=2, **every))
        self.assertEqual(result["unknown_keys"], 1)

    def test_nan_infinity_big_int_long_float_rejected(self):
        cases = [
            b'{"__REALTIME_TIMESTAMP":"%s","x":NaN}\n' % TS.encode(),
            b'{"__REALTIME_TIMESTAMP":"%s","x":Infinity}\n' % TS.encode(),
            b'{"__REALTIME_TIMESTAMP":"%s","x":-Infinity}\n' % TS.encode(),
            b'{"__REALTIME_TIMESTAMP":"%s","x":12345678901234567890}\n' % TS.encode(),
            b'{"__REALTIME_TIMESTAMP":"%s","x":1.%s}\n' % (TS.encode(), b"1" * 63),
        ]
        result = reduce_ok(b"".join(cases))
        self.assertEqual(result["malformed"], 5)
        accepted = b'{"__REALTIME_TIMESTAMP":"%s","x":1234567890123456789,"y":1.%s}\n' % (TS.encode(), b"1" * 62)
        result = reduce_ok(accepted)
        self.assertEqual(result["malformed"], 0)
        self.assertEqual(result["message_nonstr"], 1)

    def test_deep_nesting_is_malformed(self):
        data = b"[" * 100_000 + b"\n"
        result = reduce_ok(data)
        self.assertEqual(result["malformed"], 1)
        code, line = run_main(data)
        self.assertEqual(code, 0)
        self.assertEqual(parse_line(line)["status"], "OK")


class TimestampAndWindowTests(unittest.TestCase):
    def test_window_boundaries(self):
        data = b"".join([
            good(ts=str(START)), good(ts=str(START - 1)), good(ts=str(END - 1)), good(ts=str(END)),
        ])
        result = reduce_ok(data)
        self.assertEqual(result["out_of_window"], 2)
        self.assertEqual(result["events"][EVENTS[0]], 2)

    def test_ts_forms(self):
        invalid = [
            record(omit_ts=True, msg=message(EVENTS[0])),
            good(ts=""), good(ts="12ab"), good(ts="+" + TS), good(ts="-" + TS),
            good(ts="1" * 20), good(ts=TS + ".0"), good(ts="\uff11" * 16), good(ts=" " + TS),
            b'{"__REALTIME_TIMESTAMP":%s,"MESSAGE":"{}"}\n' % TS.encode(),
        ]
        result = reduce_ok(b"".join(invalid))
        self.assertEqual(result["ts_invalid"], len(invalid))
        nineteen = "0" * 3 + TS  # leading zeros, 19 digits, inside the window
        self.assertEqual(len(nineteen), 19)
        result = reduce_ok(good(ts=nineteen))
        self.assertEqual(result["ts_invalid"], 0)
        self.assertEqual(result["events"][EVENTS[0]], 1)

    def test_max_end_us_matches_datetime_max(self):
        epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
        expected = (datetime.max.replace(tzinfo=timezone.utc) - epoch) // timedelta(microseconds=1)
        self.assertEqual(reducer.MAX_END_US, expected)
        self.assertEqual(reducer.MAX_END_US, MAX_END_US)

    def test_iso_is_27_chars_and_platform_independent(self):
        self.assertEqual(reducer._iso(0), "1970-01-01T00:00:00.000000Z")
        self.assertEqual(reducer._iso(1), "1970-01-01T00:00:00.000001Z")
        self.assertEqual(reducer._iso(MAX_END_US), "9999-12-31T23:59:59.999999Z")
        for value in (0, 1, MAX_END_US):
            self.assertEqual(len(reducer._iso(value)), 27)
        # Padding is explicit in the f-string, so it does not depend on the
        # platform strftime; a window starting at 0 renders through reduce too.
        from_zero = reducer.reduce(good(ts="1"), 0, 10)
        self.assertEqual(from_zero["observed_span"]["first"], "1970-01-01T00:00:00.000001Z")

    def test_observed_span_is_min_max_over_in_window(self):
        data = b"".join([
            good(ts=str(START + 30)),
            record(ts=str(START + 10), raw=5),                 # in-window but message_nonstr
            good(ts=str(START + 20)),
            good(ts=str(START - 5)),                            # out of window, ignored
        ])
        result = reduce_ok(data)
        self.assertEqual(result["observed_span"]["first"], reducer._iso(START + 10))
        self.assertEqual(result["observed_span"]["last"], reducer._iso(START + 30))
        empty = reduce_ok(good(ts=str(END)))
        self.assertEqual(empty["observed_span"], {"first": None, "last": None})


class FramingTests(unittest.TestCase):
    def test_cr_is_malformed(self):
        result = reduce_ok(good()[:-1] + b"\r\n")
        self.assertEqual(result["malformed"], 1)
        self.assertEqual(sum(result["events"].values()), 0)
        embedded = b'{"__REALTIME_TIMESTAMP":"%s",\r"MESSAGE":"{}"}\n' % TS.encode()
        self.assertEqual(reduce_ok(embedded)["malformed"], 1)

    def test_unterminated_tail_is_malformed_and_parser_not_called(self):
        shim = CountingLoads()
        reducer._loads = shim
        try:
            terminated = good(EVENTS[0])
            tail = good(EVENTS[1])[:-1]  # valid JSON, distinct from the terminated record
            result = reduce_ok(terminated + tail)
            self.assertEqual(result["records"], 2)
            self.assertEqual(result["malformed"], 1)
            self.assertEqual(result["events"][EVENTS[0]], 1)
            self.assertEqual(result["events"][EVENTS[1]], 0)
            self.assertEqual(len(shim.calls), 2)
            self.assertEqual(shim.calls[0], terminated[:-1].decode())
            self.assertEqual(shim.calls[1], message(EVENTS[0]))
            self.assertNotIn(tail.decode(), shim.calls)
            self.assertNotIn(message(EVENTS[1]), shim.calls)

            shim.calls.clear()
            result = reduce_ok(tail)
            self.assertEqual((result["records"], result["malformed"]), (1, 1))
            self.assertEqual(shim.calls, [])

            shim.calls.clear()
            result = reduce_ok(b"")
            self.assertEqual(result["records"], 0)
            self.assertEqual(shim.calls, [])

            shim.calls.clear()
            result = reduce_ok(b"   ")
            self.assertEqual((result["records"], result["malformed"]), (1, 1))
            self.assertEqual(shim.calls, [])

            shim.calls.clear()
            result = reduce_ok(b"\n")
            self.assertEqual((result["records"], result["malformed"]), (1, 1))
            self.assertEqual(shim.calls, [""])
        finally:
            reducer._loads = shim.original

    def test_byte_cap_exact(self):
        unit = good()
        filler = b"x" * ((BYTE_CAP - 1) % len(unit))
        data = unit * ((BYTE_CAP - 1) // len(unit)) + filler + b"\n"
        self.assertEqual(len(data), BYTE_CAP)
        result = reducer.reduce(data, START, END)
        self.assertEqual(result["status"], "RECORD_CAP")  # far more than 200 records
        # Byte cap is tested with few records: pad one record to exactly the cap.
        pad = b'{"__REALTIME_TIMESTAMP":"%s","pad":"' % TS.encode()
        body = pad + b"p" * (BYTE_CAP - len(pad) - 3) + b'"}\n'
        self.assertEqual(len(body), BYTE_CAP)
        result = reducer.reduce(body, START, END)
        self.assertEqual(result["status"], "OK")
        self.assertEqual(result["bytes"], BYTE_CAP)
        self.assertEqual(result["message_nonstr"], 1)
        self.assertEqual(reducer.reduce(body + b"\n", START, END), {"status": "OVERFLOW"})
        self.assertEqual(reducer.BYTE_CAP, BYTE_CAP)

    def test_record_cap_exact(self):
        data = good() * RECORD_CAP
        result = reduce_ok(data)
        self.assertTrue(result["saturated"])
        self.assertEqual(result["records"], RECORD_CAP)
        self.assertEqual(result["events"][EVENTS[0]], RECORD_CAP)
        self.assertEqual(reducer.reduce(data + b"garbage\n", START, END), {"status": "RECORD_CAP"})
        self.assertEqual(reducer.reduce(data + b"garbage", START, END), {"status": "RECORD_CAP"})
        below = reduce_ok(good() * (RECORD_CAP - 1))
        self.assertFalse(below["saturated"])
        self.assertEqual(reducer.RECORD_CAP, RECORD_CAP)

    def test_overcap_stdin_subprocess_exits(self):
        data = good() * (3 * 1024 * 1024 // len(good()) + 1)
        self.assertGreater(len(data), 3 * 1024 * 1024)
        code, out = run_subprocess(data)
        self.assertEqual(code, 0)
        self.assertEqual(out, b'{"status":"OVERFLOW"}\n')


class SchemaTests(unittest.TestCase):
    def test_ok_schema_exact(self):
        result = reduce_ok(good(ledger_id="x"))
        self.assertEqual(set(result), OK_KEYS)
        self.assertEqual(reducer.OK_KEYS, OK_KEYS)
        self.assertEqual(tuple(sorted(result["events"])), EVENTS)
        self.assertEqual(tuple(sorted(result["keys"])), KEYS)
        self.assertEqual(tuple(sorted(reducer.EVENTS)), EVENTS)
        self.assertEqual(tuple(sorted(reducer.KEYS)), KEYS)
        self.assertEqual(tuple(reducer.COUNTERS), COUNTERS)
        for name in COUNTERS + ("bytes", "records", "unknown_events", "unknown_keys"):
            self.assertIs(type(result[name]), int, name)
        self.assertIs(type(result["saturated"]), bool)
        self.assertEqual(set(result["observed_span"]), {"first", "last"})
        for value in list(result["events"].values()) + list(result["keys"].values()):
            self.assertIs(type(value), int)
        line = reducer.render(result)
        self.assertEqual(set(parse_line(line)), OK_KEYS)

    def test_singleton_schemas_exact(self):
        self.assertEqual(run_main(b"", argv=["1"])[1], b'{"status":"USAGE"}\n')
        self.assertEqual(run_main(b"x" * (BYTE_CAP + 1))[1], b'{"status":"OVERFLOW"}\n')
        self.assertEqual(run_main(b"\n" * (RECORD_CAP + 1))[1], b'{"status":"RECORD_CAP"}\n')
        original = reducer._loads
        reducer._loads = lambda text: (_ for _ in ()).throw(RuntimeError("forced"))
        try:
            self.assertEqual(run_main(good())[1], b'{"status":"INTERNAL_ERROR"}\n')
        finally:
            reducer._loads = original

    def test_usage_forms(self):
        class Untouchable(io.BytesIO):
            def read(self, *args):
                raise AssertionError("stdin must not be read on USAGE")

        bad = [
            [], [str(START)], [str(START), str(END), "3"],
            [str(START), str(START)], [str(END), str(START)],
            [str(START), str(MAX_END_US + 1)],
            ["+" + str(START), str(END)], ["-1", str(END)],
            [" " + str(START), str(END)], [str(START), str(END) + "\n"],
            ["\uff11" * 5, str(END)], ["", str(END)], ["1" * 20, "1" * 20 + "1"],
            ["1.0", str(END)],
        ]
        for argv in bad:
            out = io.BytesIO()
            code = reducer.main(argv, stdin=Untouchable(b"data"), stdout=out)
            self.assertEqual(code, 0, argv)
            self.assertEqual(out.getvalue(), b'{"status":"USAGE"}\n', argv)
        self.assertEqual(reducer.parse_args(["0", str(MAX_END_US)]), (0, MAX_END_US))
        self.assertEqual(reducer.parse_args(["0" * 19, "1"]), (0, 1))

    def test_conservation_holds_on_mixed_input(self):
        data = b"".join([
            good(), good(EVENTS[3], ledger_id="a"), good(EVENTS[5], foreign=1),
            record(msg=message("unknown_one")), record(msg=message("unknown_two")),
            b"garbage\n", good()[:-1] + b"\r\n",
            b'{"__REALTIME_TIMESTAMP":"%s","a":1,"a":2}\n' % TS.encode(),
            good(ts="bad"), good(ts=str(END)),
            record(raw=1), record(msg="plain"),
            record(msg=json.dumps({"event": 5})),
        ])
        result = reduce_ok(data)
        for name in COUNTERS:
            self.assertGreaterEqual(result[name], 1, name)
        self.assertEqual(result["unknown_events"], 2)
        self.assertEqual(result["unknown_keys"], 1)
        self.assertTrue(conservation_holds(result))

    def test_line_bound_constant_and_worst_case(self):
        self.assertEqual(reducer.LINE_BOUND, 4096)
        worst = {
            "status": "OK", "bytes": BYTE_CAP, "records": RECORD_CAP, "saturated": True,
            "observed_span": {"first": reducer._iso(MAX_END_US), "last": reducer._iso(MAX_END_US)},
            "events": {name: RECORD_CAP for name in EVENTS},
            "keys": {name: RECORD_CAP for name in KEYS},
            "unknown_events": RECORD_CAP, "unknown_keys": RECORD_CAP,
        }
        for name in COUNTERS:
            worst[name] = RECORD_CAP
        self.assertEqual(set(worst), OK_KEYS)
        line = reducer.render(worst)
        self.assertLessEqual(len(line), reducer.LINE_BOUND)
        self.assertLessEqual(len(line), 4096)
        self.assertTrue(line.endswith(b"\n") and line.count(b"\n") == 1)

        every = {name: "v" for name in KEYS}
        spread = b"".join(
            good(EVENTS[i % len(EVENTS)], ts=str(START + i), foreign=i, **every) for i in range(RECORD_CAP)
        )
        code, out = run_main(spread)
        self.assertEqual(code, 0)
        self.assertLessEqual(len(out), 4096)
        self.assertTrue(out.endswith(b"\n") and out.count(b"\n") == 1)
        result = parse_line(out)
        self.assertTrue(result["saturated"])
        self.assertEqual(sum(result["events"].values()), RECORD_CAP)
        self.assertEqual(result["unknown_keys"], RECORD_CAP)

        with self.assertRaises(Exception):
            reducer.render({"status": "OK", "pad": "p" * 5000})
        original = reducer.reduce
        reducer.reduce = lambda data, start, end: {"status": "OK", "pad": "p" * 5000}
        try:
            self.assertEqual(run_main(good())[1], b'{"status":"INTERNAL_ERROR"}\n')
        finally:
            reducer.reduce = original


class FailureModeTests(unittest.TestCase):
    def test_forced_exception_is_singleton_internal_error(self):
        original = reducer._loads

        def raiser(text):
            raise RuntimeError("forced")

        reducer._loads = raiser
        try:
            code, line = run_main(good())
            self.assertEqual(code, 0)
            self.assertEqual(line, b'{"status":"INTERNAL_ERROR"}\n')

            def interrupt(text):
                raise KeyboardInterrupt()

            reducer._loads = interrupt
            with self.assertRaises(KeyboardInterrupt):
                run_main(good())
        finally:
            reducer._loads = original

    def test_exit_code_policy(self):
        class Broken(io.BytesIO):
            def write(self, data):
                raise OSError("pipe closed")

        self.assertEqual(reducer.main(ARGV, stdin=io.BytesIO(good()), stdout=Broken()), 1)
        self.assertEqual(reducer.main(["1"], stdin=io.BytesIO(b""), stdout=Broken()), 1)
        self.assertEqual(run_main(good())[0], 0)
        self.assertEqual(run_main(b"", argv=[])[0], 0)
        self.assertEqual(run_main(b"x" * (BYTE_CAP + 1))[0], 0)

    def test_seeded_fuzz(self):
        rng = random.Random(20260916)
        alphabet = "abcdefghijklmnopqrstuvwxyz_\"\\{}[]:,0123456789\u00e9\u4e2d"

        def scalar(canary):
            choice = rng.randrange(6)
            if choice == 0:
                return canary
            if choice == 1:
                return rng.randrange(-10, 10 ** 22)
            if choice == 2:
                return rng.random() * 10 ** rng.randrange(0, 40)
            if choice == 3:
                return None
            if choice == 4:
                return rng.choice([True, False])
            return "".join(rng.choice(alphabet) for _ in range(rng.randrange(0, 12)))

        def nested(depth, canary):
            if depth <= 0 or rng.random() < 0.4:
                return scalar(canary)
            if rng.random() < 0.5:
                return [nested(depth - 1, canary) for _ in range(rng.randrange(0, 4))]
            keys = [rng.choice(list(KEYS) + ["event", "foreign", canary[:6]]) for _ in range(rng.randrange(0, 5))]
            return "{" + ",".join(json.dumps(k) + ":" + json.dumps(nested(depth - 1, canary)) for k in keys) + "}"

        def one_record(i, canary):
            kind = rng.randrange(8)
            if kind == 0:
                return bytes(rng.randrange(256) for _ in range(rng.randrange(0, 80))) + b"\n"
            ts = rng.choice([str(START + rng.randrange(0, 3_600_000_000)), canary, "", "1" * 20, str(END)])
            event = rng.choice(list(EVENTS) + [canary, "unknown"])
            fields = {}
            for _ in range(rng.randrange(0, 4)):
                fields[rng.choice(list(KEYS) + ["foreign", canary[:8]])] = scalar(canary)
            if kind == 1:
                inner = nested(3, canary)
                inner = inner if isinstance(inner, str) else json.dumps(inner)
            elif kind == 2:
                inner = json.dumps({"event": event, **fields})
            else:
                inner = message(event, **fields)
            outer = {"__REALTIME_TIMESTAMP": ts, "MESSAGE": inner}
            if kind == 3:
                outer["MESSAGE"] = [rng.randrange(256) for _ in range(5)]
            if kind == 4:
                outer[canary[:5]] = nested(2, canary)
            text = json.dumps(outer)
            if kind == 5:
                text = text.replace('"MESSAGE"', '"MESSAGE":1,"MESSAGE"', 1)
            if kind == 6:
                text = text[: rng.randrange(0, len(text))]
            return text.encode("utf-8") + (b"\r\n" if rng.random() < 0.1 else b"\n")

        for i in range(300):
            canary = "SECRET_%d_%08x" % (i, rng.getrandbits(32))
            data = b"".join(one_record(i, canary) for _ in range(rng.randrange(0, 12)))
            if rng.random() < 0.2:
                data = data.rstrip(b"\n")
            code, line = run_main(data)
            self.assertEqual(code, 0)
            self.assertTrue(line.endswith(b"\n") and line.count(b"\n") == 1, line)
            self.assertLessEqual(len(line), 4096)
            self.assertNotIn(canary.encode("utf-8"), line)
            self.assertNotIn(b"SECRET_", line)
            result = parse_line(line)
            if result["status"] == "OK":
                self.assertTrue(conservation_holds(result), result)
            else:
                self.assertEqual(set(result), {"status"})

    def test_output_uses_lf_only_on_windows(self):
        code, out = run_subprocess(good())
        self.assertEqual(code, 0)
        self.assertNotIn(b"\r", out)
        self.assertTrue(out.endswith(b"\n") and out.count(b"\n") == 1)
        self.assertEqual(parse_line(out)["events"][EVENTS[0]], 1)


FORBIDDEN_NAMES = {"open", "print", "eval", "exec", "compile", "__import__", "input", "breakpoint"}
PRODUCTION_LITERALS = ("journalctl", "systemctl", "gecko-pipeline", ".service", "/root/", "2026-09-14", "20:45", "21:45")


class StaticContractTests(unittest.TestCase):
    def setUp(self):
        self.raw = REDUCER_PATH.read_bytes()
        self.source = self.raw.decode("utf-8")
        self.tree = ast.parse(self.source)

    def test_import_allowlist(self):
        modules = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                modules.add(node.module)
        self.assertEqual(modules, {"json", "re", "sys", "datetime"})

    def test_forbidden_builtins_and_attributes(self):
        names = {n.id for n in ast.walk(self.tree) if isinstance(n, ast.Name)}
        self.assertEqual(names & FORBIDDEN_NAMES, set())
        attrs = {n.attr for n in ast.walk(self.tree) if isinstance(n, ast.Attribute)}
        self.assertEqual(attrs & {"system", "popen", "Popen", "run", "fork", "spawn", "environ"}, set())

    def test_no_production_literals(self):
        for literal in PRODUCTION_LITERALS:
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
        code, out = run_subprocess(b"", argv=[])
        self.assertEqual(code, 0)
        self.assertEqual(out, b'{"status":"USAGE"}\n')


if __name__ == "__main__":
    unittest.main()
