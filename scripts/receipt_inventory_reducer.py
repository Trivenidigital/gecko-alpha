"""Receipt inventory reducer: pure, offline, allowlisted reduction of journal JSON.

Stdlib only; spawns nothing; reads stdin bytes and writes exactly one
newline-terminated JSON line to stdout. Invoked as::

    python3 -I -S scripts/receipt_inventory_reducer.py START_US END_US

START_US and END_US are epoch microseconds; the window is ``start <= ts < end``.
No value from the input is ever printed: the output carries fixed schema names,
fixed event and key names, counts, and two timestamps derived from validated
digit strings. Design: tasks/design_receipt_reducer_meta_2026_09_16.md.
"""

import json
import re
import sys
from datetime import datetime, timedelta, timezone

BYTE_CAP = 2_097_152
RECORD_CAP = 200
LINE_BOUND = 4_096
MAX_END_US = 253_402_300_799_999_999  # last microsecond of year 9999
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

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
STRUCTURAL_KEYS = frozenset({"event"})
COUNTERS = (
    "malformed", "duplicate_key", "ts_invalid", "out_of_window",
    "message_nonstr", "message_nonjson", "event_invalid",
)
OK_KEYS = frozenset(
    ("status", "bytes", "records", "saturated", "observed_span", "events", "keys",
     "unknown_events", "unknown_keys") + COUNTERS
)
SINGLETON_STATUSES = ("USAGE", "OVERFLOW", "RECORD_CAP", "INTERNAL_ERROR")

_EVENT_SET = frozenset(EVENTS)
_KEY_SET = frozenset(KEYS)
_TS = re.compile(r"[0-9]{1,19}")
_MAX_INT_CHARS = 19
_MAX_FLOAT_CHARS = 64
_INTERNAL_ERROR_LINE = b'{"status":"INTERNAL_ERROR"}\n'


class _DuplicateKey(Exception):
    """A JSON object repeated a key. Deliberately not a ValueError."""


class _Rejected(Exception):
    """A JSON token outside the bounded grammar (NaN, Infinity, long numbers)."""


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey()
        result[key] = value
    return result


def _reject_constant(name):
    raise _Rejected()


def _parse_int(text):
    if len(text) > _MAX_INT_CHARS:
        raise _Rejected()
    return int(text)


def _parse_float(text):
    if len(text) > _MAX_FLOAT_CHARS:
        raise _Rejected()
    return float(text)


def _loads(text):
    """Bounded JSON parse. Module-level so tests can replace or count it."""
    return json.loads(
        text,
        object_pairs_hook=_pairs,
        parse_constant=_reject_constant,
        parse_int=_parse_int,
        parse_float=_parse_float,
    )


# Every parse failure class that is classified rather than reported as INTERNAL_ERROR.
_PARSE_FAILURES = (json.JSONDecodeError, RecursionError, _Rejected, UnicodeDecodeError, ValueError)


def _iso(ts_us):
    moment = EPOCH + timedelta(microseconds=ts_us)
    return (
        f"{moment.year:04d}-{moment.month:02d}-{moment.day:02d}"
        f"T{moment.hour:02d}:{moment.minute:02d}:{moment.second:02d}.{moment.microsecond:06d}Z"
    )


def _new_result():
    result = {
        "status": "OK",
        "bytes": 0,
        "records": 0,
        "saturated": False,
        "observed_span": {"first": None, "last": None},
        "events": {name: 0 for name in EVENTS},
        "keys": {name: 0 for name in KEYS},
        "unknown_events": 0,
        "unknown_keys": 0,
    }
    for name in COUNTERS:
        result[name] = 0
    return result


def _classify(segment, terminated, start_us, end_us, result, span):
    """Classify one record into exactly one counter, one event, or unknown_events."""
    if not terminated:
        # Unterminated tail: truncation evidence, never handed to the parser.
        result["malformed"] += 1
        return
    if b"\r" in segment:
        result["malformed"] += 1
        return
    try:
        text = segment.decode("utf-8")
    except UnicodeDecodeError:
        result["malformed"] += 1
        return
    try:
        outer = _loads(text)
    except _DuplicateKey:
        result["duplicate_key"] += 1
        return
    except _PARSE_FAILURES:
        result["malformed"] += 1
        return
    if not isinstance(outer, dict):
        result["malformed"] += 1
        return
    ts_text = outer.get("__REALTIME_TIMESTAMP")
    if not isinstance(ts_text, str) or _TS.fullmatch(ts_text) is None:
        result["ts_invalid"] += 1
        return
    ts_us = int(ts_text)
    if not start_us <= ts_us < end_us:
        result["out_of_window"] += 1
        return
    if span[0] is None or ts_us < span[0]:
        span[0] = ts_us
    if span[1] is None or ts_us > span[1]:
        span[1] = ts_us
    message = outer.get("MESSAGE")
    if not isinstance(message, str):
        result["message_nonstr"] += 1
        return
    try:
        inner = _loads(message)
    except _DuplicateKey:
        result["duplicate_key"] += 1
        return
    except _PARSE_FAILURES:
        result["message_nonjson"] += 1
        return
    if not isinstance(inner, dict):
        result["message_nonjson"] += 1
        return
    event = inner.get("event")
    if not isinstance(event, str):
        result["event_invalid"] += 1
        return
    if event not in _EVENT_SET:
        result["unknown_events"] += 1
        return
    result["events"][event] += 1
    unknown = False
    for key in inner:
        if key in _KEY_SET:
            result["keys"][key] += 1
        elif key not in STRUCTURAL_KEYS:
            unknown = True
    if unknown:
        result["unknown_keys"] += 1


def reduce(data, start_us, end_us):
    """Pure reduction of at most BYTE_CAP + 1 bytes to a fixed-schema dict."""
    if len(data) > BYTE_CAP:
        return {"status": "OVERFLOW"}
    has_tail = bool(data) and not data.endswith(b"\n")
    records = data.count(b"\n") + (1 if has_tail else 0)
    if records > RECORD_CAP:
        return {"status": "RECORD_CAP"}
    result = _new_result()
    result["bytes"] = len(data)
    result["records"] = records
    result["saturated"] = records == RECORD_CAP
    span = [None, None]
    if data:
        parts = data.split(b"\n")
        for segment in parts[:-1]:
            _classify(segment, True, start_us, end_us, result, span)
        if parts[-1]:
            _classify(parts[-1], False, start_us, end_us, result, span)
    result["observed_span"] = {
        "first": _iso(span[0]) if span[0] is not None else None,
        "last": _iso(span[1]) if span[1] is not None else None,
    }
    return result


def render(result):
    line = (json.dumps(result, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(line) > LINE_BOUND:
        raise ValueError("line bound exceeded")
    return line


def parse_args(argv):
    if len(argv) != 2:
        return None
    for item in argv:
        if not isinstance(item, str) or _TS.fullmatch(item) is None:
            return None
    start_us, end_us = int(argv[0]), int(argv[1])
    if not start_us < end_us <= MAX_END_US:
        return None
    return start_us, end_us


def _read(stream, limit):
    """Read at most limit + 1 bytes; every iteration adds a byte or stops."""
    wanted = limit + 1
    chunks = []
    total = 0
    while total < wanted:
        chunk = stream.read(wanted - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def main(argv, stdin=None, stdout=None):
    try:
        window = parse_args(argv)
        if window is None:
            line = render({"status": "USAGE"})
        else:
            if stdin is None:
                stdin = sys.stdin.buffer
            data = _read(stdin, BYTE_CAP)
            line = render(reduce(data, window[0], window[1]))
    except Exception:  # noqa: BLE001 - any failure is reported as the fixed singleton
        line = _INTERNAL_ERROR_LINE
    try:
        if stdout is None:
            stdout = sys.stdout.buffer
        stdout.write(line)
        stdout.flush()
    except Exception:  # noqa: BLE001 - the line could not be written
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
