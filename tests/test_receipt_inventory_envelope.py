"""Consumer rule as code, and the Linux round trip under the merged supervisor.

Design: tasks/design_receipt_reducer_meta_2026_09_16.md (revision 2), section
"Consumer rule, tests and falsifiers". ``usable_envelope`` is the executable
form of the usability rule: it rejects every envelope that is not a complete,
consistent supervisor ``OK`` line carrying a complete, consistent reducer or
META ``OK`` line. It does not authorize collection.

The negative oracle runs on every platform. Only supervisor execution is
Linux-only; those cases skip on Windows and the skip is not cleanup proof.
"""

import base64
import hashlib
import importlib.util
import json
import math
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SUPERVISOR_PATH = ROOT / "scripts" / "receipt_inventory_supervisor.py"
REDUCER_PATH = ROOT / "scripts" / "receipt_inventory_reducer.py"
META_PATH = ROOT / "scripts" / "receipt_inventory_meta.py"
HARNESS_PATH = ROOT / "tests" / "test_receipt_inventory_timeout.py"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


supervisor = load("receipt_envelope_supervisor", SUPERVISOR_PATH)
reducer = load("receipt_envelope_reducer", REDUCER_PATH)
meta = load("receipt_envelope_meta", META_PATH)
harness = load("receipt_envelope_harness", HARNESS_PATH)

# Literal pins: the oracle does not move when a script drifts.
STATUS_KEYS = frozenset((
    "status", "exit_code", "pgid", "inner_exit", "inner_elapsed", "total_elapsed",
    "reaped", "kills", "signals_received", "signal_phase", "teardowns",
    "cleanup_proof", "dropped", "overflow", "error", "survivors", "output",
))
EVENTS = frozenset((
    "ledger_coverage_check_failed", "ledger_emission_recorded", "ledger_label_pass",
    "ledger_price_lookup_failed", "ledger_record_failed", "ledger_record_rollback_failed",
    "ledger_record_skipped_db_closed", "trade_decision_event_emit_failed",
    "trade_decision_event_emitted", "trade_decision_event_rollback_failed",
    "trade_decision_event_skipped_db_closed", "trade_decision_events_pruned",
    "volume_history_cg_pruned",
))
KEYS = frozenset((
    "decision", "event_id", "kind", "ledger_id", "level", "reason",
    "signal_type", "site", "surface", "timestamp", "token_id",
))
COUNTERS = ("malformed", "duplicate_key", "ts_invalid", "out_of_window", "message_nonstr", "message_nonjson", "event_invalid")
REDUCER_OK_KEYS = frozenset(("status", "bytes", "records", "saturated", "observed_span", "events", "keys", "unknown_events", "unknown_keys") + COUNTERS)
META_FULL_KEYS = frozenset(("status", "format_ok", "head_ok", "active_state_ok", "standard_output_ok", "head", "active_state", "standard_output", "files"))
SLOTS = ("f0", "f1", "f2", "f3", "f4", "f5", "f6", "f7")
SLOT_KEYS = frozenset(("status", "sha256", "size"))
ACTIVE_STATES = frozenset({"active", "reloading", "inactive", "failed", "activating", "deactivating", "maintenance"})
STANDARD_OUTPUTS = frozenset({"inherit", "null", "tty", "journal", "kmsg", "journal+console", "kmsg+console", "socket"})
BYTE_CAP = 2_097_152
RECORD_CAP = 200
FILE_CAP = 16 * 1024 * 1024
OUTER_LINE_CAP = 131_072
INNER_LINE_CAP = 4_096
ISO = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
SCHEMAS = ("reducer", "meta")

START = 1_600_000_000_000_000
END = START + 3_600_000_000


class _Duplicate(Exception):
    pass


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Duplicate()
        result[key] = value
    return result


def _parse(data):
    return json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)


def _is_int(value):
    return type(value) is int


def _is_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _reducer_inner(inner):
    """Checks 14 to 20 for schema='reducer'."""
    if set(inner) != REDUCER_OK_KEYS:
        return "INNER_KEYS"
    for name, expected in (("events", EVENTS), ("keys", KEYS), ("observed_span", {"first", "last"})):
        if not isinstance(inner[name], dict) or set(inner[name]) != set(expected):
            return "INNER_KEYS"
    integers = ("bytes", "records", "unknown_events", "unknown_keys") + COUNTERS
    if not isinstance(inner["status"], str) or not all(_is_int(inner[n]) for n in integers):
        return "INNER_TYPES"
    if type(inner["saturated"]) is not bool:
        return "INNER_TYPES"
    if not all(_is_int(v) for v in inner["events"].values()) or not all(_is_int(v) for v in inner["keys"].values()):
        return "INNER_TYPES"
    span = inner["observed_span"]
    if not all(v is None or isinstance(v, str) for v in span.values()):
        return "INNER_TYPES"
    if inner["status"] != "OK":
        return "INNER_STATUS"
    records = inner["records"]
    if not 0 <= inner["bytes"] <= BYTE_CAP or not 0 <= records <= RECORD_CAP:
        return "INNER_DOMAIN"
    bounded = [inner[n] for n in COUNTERS] + [inner["unknown_events"], inner["unknown_keys"]]
    bounded += list(inner["events"].values()) + list(inner["keys"].values())
    if not all(0 <= v <= records for v in bounded):
        return "INNER_DOMAIN"
    events = sum(inner["events"].values())
    if records != sum(inner[n] for n in COUNTERS) + events + inner["unknown_events"]:
        return "INNER_CONSERVATION"
    if any(v > events for v in inner["keys"].values()) or inner["unknown_keys"] > events:
        return "INNER_CONSERVATION"
    if inner["saturated"] != (records == RECORD_CAP):
        return "INNER_SATURATED"
    first, last = span["first"], span["last"]
    if (first is None) != (last is None):
        return "INNER_SPAN"
    if first is None:
        if events + inner["unknown_events"] + inner["message_nonstr"] + inner["message_nonjson"] + inner["event_invalid"] != 0:
            return "INNER_SPAN"
    else:
        if ISO.fullmatch(first) is None or ISO.fullmatch(last) is None:
            return "INNER_SPAN"
        try:
            parsed = [datetime.strptime(v, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc) for v in (first, last)]
        except ValueError:
            return "INNER_SPAN"
        if parsed[0] > parsed[1]:
            return "INNER_SPAN"
        if records - inner["malformed"] - inner["ts_invalid"] - inner["out_of_window"] < 1:
            return "INNER_SPAN"
    return None


def _meta_inner(inner):
    """Checks 14 to 16 and 21 to 23 for schema='meta'."""
    if set(inner) != META_FULL_KEYS:
        return "INNER_KEYS"
    files = inner["files"]
    if not isinstance(files, dict) or tuple(files) != SLOTS:
        return "INNER_KEYS"
    for slot in files.values():
        if not isinstance(slot, dict) or set(slot) != SLOT_KEYS:
            return "INNER_KEYS"
    if not isinstance(inner["status"], str):
        return "INNER_TYPES"
    for flag in ("format_ok", "head_ok", "active_state_ok", "standard_output_ok"):
        if type(inner[flag]) is not bool:
            return "INNER_TYPES"
    for field in ("head", "active_state", "standard_output"):
        if inner[field] is not None and not isinstance(inner[field], str):
            return "INNER_TYPES"
    for slot in files.values():
        if not isinstance(slot["status"], str):
            return "INNER_TYPES"
        if slot["sha256"] is not None and not isinstance(slot["sha256"], str):
            return "INNER_TYPES"
        if slot["size"] is not None and not _is_int(slot["size"]):
            return "INNER_TYPES"
    if inner["status"] != "OK":
        return "INNER_STATUS"
    if not all(inner[flag] is True for flag in ("format_ok", "head_ok", "active_state_ok", "standard_output_ok")):
        return "INNER_META_FLAGS"
    if inner["head"] is None or HEX40.fullmatch(inner["head"]) is None:
        return "INNER_META_VALUES"
    if inner["active_state"] not in ACTIVE_STATES or inner["standard_output"] not in STANDARD_OUTPUTS:
        return "INNER_META_VALUES"
    statuses = [files[name]["status"] for name in SLOTS]
    used = 0
    while used < len(SLOTS) and statuses[used] == "OK":
        used += 1
    if not 1 <= used <= 8:
        return "INNER_SLOTS"
    for name in SLOTS[:used]:
        slot = files[name]
        if slot["sha256"] is None or HEX64.fullmatch(slot["sha256"]) is None:
            return "INNER_SLOTS"
        if slot["size"] is None or not 0 <= slot["size"] <= FILE_CAP:
            return "INNER_SLOTS"
    for name in SLOTS[used:]:
        slot = files[name]
        if slot["status"] != "UNUSED" or slot["sha256"] is not None or slot["size"] is not None:
            return "INNER_SLOTS"
    return None


def usable_envelope(returncode, line, schema):
    """Return (True, "OK") only for a usable envelope; otherwise (False, REASON)."""
    if schema not in SCHEMAS:
        raise ValueError("schema must be one of %r" % (SCHEMAS,))
    if type(returncode) is not int or returncode != 0:
        return False, "RETURNCODE"
    if type(line) is not bytes or not line.endswith(b"\n") or line.count(b"\n") != 1:
        return False, "NOT_ONE_LINE"
    if len(line) > OUTER_LINE_CAP:
        return False, "LINE_TOO_LONG"
    try:
        outer = _parse(line)
    except _Duplicate:
        return False, "OUTER_DUPLICATE_KEY"
    except (ValueError, RecursionError):
        return False, "OUTER_JSON"
    if not isinstance(outer, dict):
        return False, "OUTER_JSON"
    if set(outer) != STATUS_KEYS:
        return False, "OUTER_KEYS"
    integers = ("exit_code", "pgid", "inner_exit", "reaped", "kills", "signals_received", "teardowns", "dropped")
    if not isinstance(outer["status"], str) or not all(_is_int(outer[n]) for n in integers):
        return False, "OUTER_TYPES"
    if not _is_number(outer["inner_elapsed"]) or not _is_number(outer["total_elapsed"]):
        return False, "OUTER_TYPES"
    if type(outer["cleanup_proof"]) is not bool or type(outer["overflow"]) is not bool:
        return False, "OUTER_TYPES"
    if any(outer[n] is not None for n in ("signal_phase", "error", "survivors")) or not isinstance(outer["output"], str):
        return False, "OUTER_TYPES"
    consistent = (
        outer["status"] == "OK" and outer["exit_code"] == 0 and outer["inner_exit"] == 0
        and outer["cleanup_proof"] is True and outer["dropped"] == 0 and outer["overflow"] is False
        and outer["reaped"] == 0 and outer["kills"] == 0 and outer["signals_received"] == 0
        and outer["teardowns"] == 1 and outer["pgid"] > 1
        and 0 <= outer["inner_elapsed"] <= outer["total_elapsed"]
    )
    if not consistent:
        return False, "OUTER_INVARIANTS"
    try:
        decoded = base64.b64decode(outer["output"], validate=True)
    except ValueError:
        return False, "OUTPUT_BASE64"
    if not decoded.endswith(b"\n") or decoded.count(b"\n") != 1:
        return False, "INNER_NOT_ONE_LINE"
    if len(decoded) > INNER_LINE_CAP:
        return False, "INNER_TOO_LONG"
    try:
        inner = _parse(decoded)
    except _Duplicate:
        return False, "INNER_DUPLICATE_KEY"
    except (ValueError, RecursionError):
        return False, "INNER_JSON"
    if not isinstance(inner, dict):
        return False, "INNER_JSON"
    reason = _reducer_inner(inner) if schema == "reducer" else _meta_inner(inner)
    if reason is not None:
        return False, reason
    return True, "OK"


# ---------------------------------------------------------------------------
# Synthetic good envelopes and single-point mutations.


def message(event, **fields):
    body = {"event": event}
    body.update(fields)
    return json.dumps(body)


def record(ts, msg):
    return json.dumps({"__REALTIME_TIMESTAMP": str(ts), "MESSAGE": msg}).encode("utf-8") + b"\n"


def reducer_fixture():
    events = sorted(EVENTS)
    parts = []
    for i in range(40):
        parts.append(record(START + i * 1000, message(events[i % len(events)], ledger_id="x", foreign=i)))
    parts.append(record(START - 1, message(events[0])))
    parts.append(record("bad", message(events[0])))
    parts.append(b"garbage\n")
    parts.append(record(START + 5, "not json"))
    parts.append(record(START + 6, message("unknown_event")))
    return b"".join(parts)


def reducer_inner_dict():
    return reducer.reduce(reducer_fixture(), START, END)


def meta_inner_dict(used=2):
    files = {}
    for index, name in enumerate(SLOTS):
        if index < used:
            files[name] = {"status": "OK", "sha256": hashlib.sha256(name.encode()).hexdigest(), "size": 5}
        else:
            files[name] = {"status": "UNUSED", "sha256": None, "size": None}
    return {
        "status": "OK", "format_ok": True, "head_ok": True, "active_state_ok": True, "standard_output_ok": True,
        "head": "a" * 40, "active_state": "active", "standard_output": "journal", "files": files,
    }


def inner_line(obj):
    return (json.dumps(obj, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")


def outer_dict(inner_bytes):
    return {
        "status": "OK", "exit_code": 0, "pgid": 4242, "inner_exit": 0, "inner_elapsed": 0.05,
        "total_elapsed": 0.1, "reaped": 0, "kills": 0, "signals_received": 0, "signal_phase": None,
        "teardowns": 1, "cleanup_proof": True, "dropped": 0, "overflow": False, "error": None,
        "survivors": None, "output": base64.b64encode(inner_bytes).decode("ascii"),
    }


def envelope(outer):
    return (json.dumps(outer, separators=(",", ":")) + "\n").encode("utf-8")


def good_inner(schema):
    return reducer_inner_dict() if schema == "reducer" else meta_inner_dict()


def good_envelope(schema):
    return envelope(outer_dict(inner_line(good_inner(schema))))


def with_outer(schema, **changes):
    outer = outer_dict(inner_line(good_inner(schema)))
    for key, value in changes.items():
        if value is ...:
            del outer[key]
        else:
            outer[key] = value
    return envelope(outer)


def with_inner(schema, mutate):
    inner = good_inner(schema)
    mutate(inner)
    return envelope(outer_dict(inner_line(inner)))


def with_inner_bytes(schema, inner_bytes):
    return envelope(outer_dict(inner_bytes))


class OracleGoodTests(unittest.TestCase):
    def test_good_envelopes_are_usable(self):
        for schema in SCHEMAS:
            self.assertEqual(usable_envelope(0, good_envelope(schema), schema), (True, "OK"))
        one_slot = meta_inner_dict(used=1)
        self.assertEqual(usable_envelope(0, with_inner_bytes("meta", inner_line(one_slot)), "meta"), (True, "OK"))
        eight = meta_inner_dict(used=8)
        self.assertEqual(usable_envelope(0, with_inner_bytes("meta", inner_line(eight)), "meta"), (True, "OK"))
        saturated = reducer.reduce(record(START, message(sorted(EVENTS)[0])) * RECORD_CAP, START, END)
        self.assertEqual(usable_envelope(0, with_inner_bytes("reducer", inner_line(saturated)), "reducer"), (True, "OK"))
        empty = reducer.reduce(b"", START, END)
        self.assertEqual(usable_envelope(0, with_inner_bytes("reducer", inner_line(empty)), "reducer"), (True, "OK"))

    def test_schema_constants_pinned(self):
        self.assertEqual(frozenset(supervisor.STATUS_KEYS), STATUS_KEYS)
        self.assertEqual(reducer.OK_KEYS, REDUCER_OK_KEYS)
        self.assertEqual(meta.FULL_KEYS, META_FULL_KEYS)
        self.assertEqual(frozenset(reducer.EVENTS), EVENTS)
        self.assertEqual(frozenset(reducer.KEYS), KEYS)
        self.assertEqual(reducer.LINE_BOUND, INNER_LINE_CAP)
        self.assertEqual(meta.LINE_BOUND, INNER_LINE_CAP)
        self.assertEqual(reducer.BYTE_CAP, BYTE_CAP)
        self.assertEqual(reducer.RECORD_CAP, RECORD_CAP)
        self.assertEqual(meta.FILE_CAP, FILE_CAP)
        self.assertEqual(supervisor.LINE_LIMIT, OUTER_LINE_CAP)

    def test_bad_schema_raises(self):
        with self.assertRaises(ValueError):
            usable_envelope(0, good_envelope("reducer"), "other")


class OracleNegativeTests(unittest.TestCase):
    """One contradiction per subTest; each asserts the exact reason."""

    def check(self, reason, cases, schema="reducer"):
        for label, returncode, line in cases:
            with self.subTest(reason=reason, case=label):
                self.assertEqual(usable_envelope(returncode, line, schema), (False, reason))

    def test_returncode(self):
        line = good_envelope("reducer")
        self.check("RETURNCODE", [
            ("eleven", 11, line), ("true", True, line), ("float", 0.0, line), ("string", "0", line), ("none", None, line),
        ])

    def test_not_one_line(self):
        line = good_envelope("reducer")
        self.check("NOT_ONE_LINE", [
            ("two_lines", 0, line + line), ("no_lf", 0, line[:-1]), ("str", 0, line.decode()), ("empty", 0, b""),
        ])

    def test_line_too_long(self):
        padded = with_outer("reducer", output=base64.b64encode(b"x" * 98_400).decode())
        self.assertGreater(len(padded), OUTER_LINE_CAP)
        self.check("LINE_TOO_LONG", [("over_cap", 0, padded)])

    def test_outer_duplicate_key(self):
        line = good_envelope("reducer")
        dup = line.replace(b'"status":"OK"', b'"status":"OK","status":"OK"', 1)
        self.check("OUTER_DUPLICATE_KEY", [("status_twice", 0, dup)])

    def test_outer_json(self):
        self.check("OUTER_JSON", [
            ("truncated", 0, good_envelope("reducer")[:-3] + b"\n"),
            ("list", 0, b"[]\n"), ("bad_utf8", 0, b"\xff\n"),
        ])

    def test_outer_keys(self):
        self.check("OUTER_KEYS", [
            ("missing", 0, with_outer("reducer", survivors=...)),
            ("extra", 0, with_outer("reducer", extra=1)),
        ])

    def test_outer_types(self):
        line = good_envelope("reducer")
        self.check("OUTER_TYPES", [
            ("cleanup_proof_int", 0, with_outer("reducer", cleanup_proof=1)),
            ("exit_code_float", 0, with_outer("reducer", exit_code=0.0)),
            ("inner_elapsed_nan", 0, line.replace(b'"inner_elapsed":0.05', b'"inner_elapsed":NaN', 1)),
            ("inner_elapsed_inf", 0, line.replace(b'"inner_elapsed":0.05', b'"inner_elapsed":Infinity', 1)),
            ("total_elapsed_str", 0, with_outer("reducer", total_elapsed="1")),
            ("error_empty_string", 0, with_outer("reducer", error="")),
            ("output_null", 0, with_outer("reducer", output=None)),
            ("dropped_bool", 0, with_outer("reducer", dropped=False)),
        ])

    def test_outer_invariants(self):
        self.check("OUTER_INVARIANTS", [
            ("dropped", 0, with_outer("reducer", dropped=1)),
            ("overflow", 0, with_outer("reducer", overflow=True)),
            ("reaped", 0, with_outer("reducer", reaped=1)),
            ("kills", 0, with_outer("reducer", kills=1)),
            ("teardowns_zero", 0, with_outer("reducer", teardowns=0)),
            ("pgid_one", 0, with_outer("reducer", pgid=1)),
            ("ok_with_exit_3", 0, with_outer("reducer", exit_code=3)),
            ("status_command_failed", 0, with_outer("reducer", status="COMMAND_FAILED")),
            ("inner_exit", 0, with_outer("reducer", inner_exit=1)),
            ("signals", 0, with_outer("reducer", signals_received=1)),
            ("cleanup_false", 0, with_outer("reducer", cleanup_proof=False)),
            ("elapsed_order", 0, with_outer("reducer", inner_elapsed=0.2, total_elapsed=0.1)),
        ])

    def test_output_base64(self):
        self.check("OUTPUT_BASE64", [
            ("not_base64", 0, with_outer("reducer", output="@@@@")),
            ("non_canonical_padding", 0, with_outer("reducer", output="YQ")),
        ])

    def test_inner_not_one_line(self):
        one = inner_line(reducer_inner_dict())
        self.check("INNER_NOT_ONE_LINE", [
            ("two_lines", 0, with_inner_bytes("reducer", one + one)),
            ("no_lf", 0, with_inner_bytes("reducer", one[:-1])),
            ("empty", 0, with_inner_bytes("reducer", b"")),
        ])

    def test_inner_too_long(self):
        big = inner_line({"status": "OK", "pad": "p" * 4_200})
        self.check("INNER_TOO_LONG", [("over_4096", 0, with_inner_bytes("reducer", big))])

    def test_inner_duplicate_key(self):
        one = inner_line(reducer_inner_dict())
        dup = one.replace(b'"status":"OK"', b'"status":"OK","status":"OK"', 1)
        self.check("INNER_DUPLICATE_KEY", [("status_twice", 0, with_inner_bytes("reducer", dup))])

    def test_inner_json(self):
        self.check("INNER_JSON", [
            ("truncated", 0, with_inner_bytes("reducer", b'{"status":"OK"\n')),
            ("list", 0, with_inner_bytes("reducer", b"[]\n")),
        ])

    def test_inner_keys(self):
        def drop(inner):
            del inner["saturated"]

        def extra(inner):
            inner["extra"] = 1

        def fourteenth(inner):
            inner["events"]["fourteenth_event"] = 0

        def slot_extra(inner):
            inner["files"]["f0"]["path"] = "x"

        def ninth(inner):
            inner["files"]["f8"] = {"status": "UNUSED", "sha256": None, "size": None}

        self.check("INNER_KEYS", [
            ("missing", 0, with_inner("reducer", drop)),
            ("extra", 0, with_inner("reducer", extra)),
            ("events_14th", 0, with_inner("reducer", fourteenth)),
            ("singleton_overflow", 0, with_inner_bytes("reducer", b'{"status":"OVERFLOW"}\n')),
            ("singleton_internal_error", 0, with_inner_bytes("reducer", b'{"status":"INTERNAL_ERROR"}\n')),
        ])
        self.check("INNER_KEYS", [
            ("slot_extra_key", 0, with_inner("meta", slot_extra)),
            ("ninth_slot", 0, with_inner("meta", ninth)),
            ("reducer_line_under_meta_schema", 0, good_envelope("reducer")),
        ], schema="meta")

    def test_inner_types(self):
        def saturated_zero(inner):
            inner["saturated"] = 0

        def records_float(inner):
            inner["records"] = 1.0

        def bytes_bool(inner):
            inner["bytes"] = True

        def sha_int(inner):
            inner["files"]["f0"]["sha256"] = 5

        def flag_int(inner):
            inner["head_ok"] = 1

        self.check("INNER_TYPES", [
            ("saturated_0", 0, with_inner("reducer", saturated_zero)),
            ("records_1.0", 0, with_inner("reducer", records_float)),
            ("bytes_true", 0, with_inner("reducer", bytes_bool)),
        ])
        self.check("INNER_TYPES", [
            ("sha256_5", 0, with_inner("meta", sha_int)),
            ("head_ok_1", 0, with_inner("meta", flag_int)),
        ], schema="meta")

    def test_inner_status(self):
        def overflow(inner):
            inner["status"] = "OVERFLOW"

        def failed(inner):
            inner["status"] = "FAILED"

        self.check("INNER_STATUS", [("overflow", 0, with_inner("reducer", overflow))])
        self.check("INNER_STATUS", [("failed", 0, with_inner("meta", failed))], schema="meta")

    def test_inner_domain(self):
        def bytes_over(inner):
            inner["bytes"] = BYTE_CAP + 1

        def records_over(inner):
            inner["records"] = RECORD_CAP + 1

        def malformed_over(inner):
            inner["malformed"] = inner["records"] + 1

        def event_over(inner):
            inner["events"]["ledger_label_pass"] = inner["records"] + 1

        def negative(inner):
            inner["ts_invalid"] = -1

        self.check("INNER_DOMAIN", [
            ("bytes_over_cap", 0, with_inner("reducer", bytes_over)),
            ("records_over_cap", 0, with_inner("reducer", records_over)),
            ("malformed_over_records", 0, with_inner("reducer", malformed_over)),
            ("event_over_records", 0, with_inner("reducer", event_over)),
            ("negative_counter", 0, with_inner("reducer", negative)),
        ])

    def test_inner_conservation(self):
        def short_sum(inner):
            inner["malformed"] -= 1
            inner["records"] -= 0  # records unchanged: sum is now one short

        def keys_over_events(inner):
            inner["keys"]["ledger_id"] = sum(inner["events"].values()) + 1

        def unknown_keys_over_events(inner):
            inner["unknown_keys"] = sum(inner["events"].values()) + 1

        self.check("INNER_CONSERVATION", [
            ("sum_one_short", 0, with_inner("reducer", short_sum)),
            ("keys_over_events", 0, with_inner("reducer", keys_over_events)),
            ("unknown_keys_over_events", 0, with_inner("reducer", unknown_keys_over_events)),
        ])

    def test_inner_saturated(self):
        base = record(START, message(sorted(EVENTS)[0]))

        def not_saturated(inner):
            inner["saturated"] = False

        def falsely_saturated(inner):
            inner["saturated"] = True

        full = reducer.reduce(base * RECORD_CAP, START, END)
        not_saturated(full)
        almost = reducer.reduce(base * (RECORD_CAP - 1), START, END)
        falsely_saturated(almost)
        self.check("INNER_SATURATED", [
            ("200_not_saturated", 0, with_inner_bytes("reducer", inner_line(full))),
            ("199_saturated", 0, with_inner_bytes("reducer", inner_line(almost))),
        ])

    def test_inner_span(self):
        def unpaired(inner):
            inner["observed_span"]["last"] = None

        def bad_iso(inner):
            inner["observed_span"]["first"] = "2020-01-01 00:00:00"

        def invalid_date(inner):
            inner["observed_span"]["first"] = "2020-13-01T00:00:00.000000Z"

        def reversed_span(inner):
            first, last = inner["observed_span"]["first"], inner["observed_span"]["last"]
            inner["observed_span"] = {"first": last, "last": first}

        def null_with_event(inner):
            inner["observed_span"] = {"first": None, "last": None}

        all_malformed = reducer.reduce(b"garbage\n" * 3, START, END)
        all_malformed["observed_span"] = {"first": "2020-01-01T00:00:00.000000Z", "last": "2020-01-01T00:00:00.000000Z"}
        self.check("INNER_SPAN", [
            ("unpaired", 0, with_inner("reducer", unpaired)),
            ("bad_iso", 0, with_inner("reducer", bad_iso)),
            ("invalid_date", 0, with_inner("reducer", invalid_date)),
            ("reversed", 0, with_inner("reducer", reversed_span)),
            ("null_with_event", 0, with_inner("reducer", null_with_event)),
            ("span_with_all_malformed", 0, with_inner_bytes("reducer", inner_line(all_malformed))),
        ])

    def test_inner_meta_flags(self):
        cases = []
        for flag in ("format_ok", "head_ok", "active_state_ok", "standard_output_ok"):
            def clear(inner, flag=flag):
                inner[flag] = False
            cases.append((flag, 0, with_inner("meta", clear)))
        self.check("INNER_META_FLAGS", cases, schema="meta")

    def test_inner_meta_values(self):
        def upper(inner):
            inner["head"] = "A" * 40

        def short(inner):
            inner["head"] = "a" * 39

        def running(inner):
            inner["active_state"] = "running"

        def file_form(inner):
            inner["standard_output"] = "file:/x"

        def null_head(inner):
            inner["head"] = None

        self.check("INNER_META_VALUES", [
            ("head_upper", 0, with_inner("meta", upper)),
            ("head_39", 0, with_inner("meta", short)),
            ("active_running", 0, with_inner("meta", running)),
            ("output_file", 0, with_inner("meta", file_form)),
            ("head_null", 0, with_inner("meta", null_head)),
        ], schema="meta")

    def test_inner_slots(self):
        def zero_used(inner):
            for name in SLOTS:
                inner["files"][name] = {"status": "UNUSED", "sha256": None, "size": None}

        def gap(inner):
            inner["files"]["f0"] = {"status": "UNUSED", "sha256": None, "size": None}

        def missing(inner):
            inner["files"]["f1"] = {"status": "MISSING", "sha256": None, "size": None}

        def short_digest(inner):
            inner["files"]["f0"]["sha256"] = "a" * 63

        def negative_size(inner):
            inner["files"]["f0"]["size"] = -1

        def unused_with_digest(inner):
            inner["files"]["f3"]["sha256"] = "a" * 64

        def size_over_cap(inner):
            inner["files"]["f0"]["size"] = FILE_CAP + 1

        def digest_null(inner):
            inner["files"]["f0"]["sha256"] = None

        self.check("INNER_SLOTS", [
            ("zero_used", 0, with_inner("meta", zero_used)),
            ("f1_used_f0_unused", 0, with_inner("meta", gap)),
            ("used_missing", 0, with_inner("meta", missing)),
            ("63_hex", 0, with_inner("meta", short_digest)),
            ("size_negative", 0, with_inner("meta", negative_size)),
            ("unused_with_digest", 0, with_inner("meta", unused_with_digest)),
            ("size_over_cap", 0, with_inner("meta", size_over_cap)),
            ("digest_null", 0, with_inner("meta", digest_null)),
        ], schema="meta")


# ---------------------------------------------------------------------------
# Linux round trip under the merged supervisor: synthetic fixtures only.

FAULT_RUNNER = '''
import importlib.util, sys
spec = importlib.util.spec_from_file_location("reducer_fault", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def raiser(text):
    raise RuntimeError("forced")


module._loads = raiser
sys.exit(module.main(sys.argv[2:]))
'''


@unittest.skipUnless(sys.platform == "linux", "requires Linux; Windows skip is not cleanup proof")
class RoundTripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for command in ("timeout", "bash", "head", "cat", "pgrep"):
            if shutil.which(command) is None:
                raise RuntimeError("missing required Linux prerequisite: %s" % command)
        version = subprocess.run(["timeout", "--version"], capture_output=True, text=True, timeout=2, check=True)
        if "GNU coreutils" not in version.stdout:
            raise RuntimeError("these tests require GNU coreutils timeout")

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="receipt-envelope-")
        self.directory = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def supervise(self, pipeline):
        pgid_file = self.directory / "wrapper.pgid"
        argv = [
            sys.executable, "-I", "-S", str(SUPERVISOR_PATH), "--pgid-file", str(pgid_file), "--",
            "timeout", "-k", "2", "10", "bash", "--noprofile", "--norc", "-o", "pipefail", "+m", "-c", pipeline,
        ]
        completed = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=20)
        pgid = int(pgid_file.read_text().strip())
        harness.assert_empty(harness.guarded_group(pgid))
        return completed.returncode, completed.stdout

    def reducer_pipeline(self, fixture):
        return "cat %s | head -c 2097153 | %s -I -S %s %d %d" % (
            shlex.quote(str(fixture)), shlex.quote(sys.executable), shlex.quote(str(REDUCER_PATH)), START, END,
        )

    def test_reducer_round_trip_ok(self):
        fixture = self.directory / "journal.jsonl"
        data = reducer_fixture()
        fixture.write_bytes(data)
        code, line = self.supervise(self.reducer_pipeline(fixture))
        self.assertEqual(code, 0, line)
        self.assertEqual(usable_envelope(code, line, "reducer"), (True, "OK"), line)
        outer = _parse(line)
        inner = _parse(base64.b64decode(outer["output"], validate=True))
        self.assertEqual(inner, reducer.reduce(data, START, END))
        self.assertEqual(inner["records"], 45)
        self.assertEqual(inner["unknown_events"], 1)
        self.assertEqual(inner["malformed"], 1)

    def test_reducer_round_trip_cap_plus_one(self):
        fixture = self.directory / "cap_plus_one.bin"
        fixture.write_bytes(b"x" * (BYTE_CAP + 1))
        code, line = self.supervise(self.reducer_pipeline(fixture))
        outer = _parse(line)
        self.assertEqual(outer["status"], "OK", line)
        self.assertEqual(base64.b64decode(outer["output"], validate=True), b'{"status":"OVERFLOW"}\n')
        self.assertEqual(usable_envelope(code, line, "reducer"), (False, "INNER_KEYS"))

    def test_reducer_round_trip_cap_plus_1mib(self):
        fixture = self.directory / "cap_plus_mib.bin"
        fixture.write_bytes(b"x" * (BYTE_CAP + 1_048_576))
        code, line = self.supervise(self.reducer_pipeline(fixture))
        outer = _parse(line)
        self.assertEqual(outer["status"], "COMMAND_FAILED", line)
        self.assertIsNone(outer["output"])
        self.assertNotEqual(code, 0)
        usable, reason = usable_envelope(code, line, "reducer")
        self.assertFalse(usable)
        self.assertEqual(reason, "RETURNCODE")
        usable, reason = usable_envelope(0, line, "reducer")
        self.assertFalse(usable)
        self.assertIn(reason, ("OUTER_TYPES", "OUTER_INVARIANTS"), "pipefail interaction: inner_exit=%r" % outer["inner_exit"])

    def test_reducer_forced_raise(self):
        fixture = self.directory / "journal.jsonl"
        fixture.write_bytes(reducer_fixture())
        runner = self.directory / "fault_runner.py"
        runner.write_text(FAULT_RUNNER, encoding="utf-8")
        pipeline = "cat %s | head -c 2097153 | %s -I -S %s %s %d %d" % (
            shlex.quote(str(fixture)), shlex.quote(sys.executable), shlex.quote(str(runner)),
            shlex.quote(str(REDUCER_PATH)), START, END,
        )
        code, line = self.supervise(pipeline)
        outer = _parse(line)
        self.assertEqual(outer["status"], "OK", line)
        self.assertEqual(base64.b64decode(outer["output"], validate=True), b'{"status":"INTERNAL_ERROR"}\n')
        self.assertEqual(usable_envelope(code, line, "reducer"), (False, "INNER_KEYS"))

    def test_meta_round_trip_ok(self):
        root = self.directory / "root"
        root.mkdir()
        a = b"alpha\n"
        b = b"beta\n"
        (root / "a.txt").write_bytes(a)
        (root / "b.txt").write_bytes(b)
        fixture = self.directory / "metadata.txt"
        fixture.write_bytes(("b" * 40 + "\nActiveState=active\nStandardOutput=journal\n").encode("ascii"))
        pipeline = "cat %s | head -c 4097 | %s -I -S %s %s a.txt b.txt" % (
            shlex.quote(str(fixture)), shlex.quote(sys.executable), shlex.quote(str(META_PATH)),
            shlex.quote(str(root.resolve())),
        )
        code, line = self.supervise(pipeline)
        self.assertEqual(usable_envelope(code, line, "meta"), (True, "OK"), line)
        inner = _parse(base64.b64decode(_parse(line)["output"], validate=True))
        self.assertEqual(inner["files"]["f0"]["sha256"], hashlib.sha256(a).hexdigest())
        self.assertEqual(inner["files"]["f1"]["sha256"], hashlib.sha256(b).hexdigest())
        self.assertEqual(inner["files"]["f2"]["status"], "UNUSED")


if __name__ == "__main__":
    unittest.main()
