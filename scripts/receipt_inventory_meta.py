"""Receipt inventory META: pure, offline validation of host identity metadata.

Stdlib only; spawns nothing; reads at most 4,097 bytes of metadata from stdin,
hashes an explicit bounded list of regular files beneath an explicit root, and
writes exactly one newline-terminated JSON line to stdout. Invoked as::

    python3 -I -S scripts/receipt_inventory_meta.py ROOT REL [REL ...]

ROOT is an absolute directory; each REL is a bounded relative path (at most
eight). Metadata is three lines: a 40-hex revision, then ``ActiveState=`` and
``StandardOutput=`` lines in either order. Output values are members of fixed
sets, validated hex, counts, or null; raw input text is never echoed.
Design: tasks/design_receipt_reducer_meta_2026_09_16.md.
"""

import errno
import hashlib
import json
import os
import re
import stat
import sys

INPUT_CAP = 4_096
FILE_CAP = 16 * 1024 * 1024
CHUNK = 65_536
READ_ITERATIONS = 4 * (FILE_CAP // CHUNK) + 2
LINE_BOUND = 4_096
SLOTS = ("f0", "f1", "f2", "f3", "f4", "f5", "f6", "f7")
PATH_BOUND = 256
ROOT_BOUND = 1_024
ACTIVE_STATES = frozenset({
    "active", "reloading", "inactive", "failed", "activating", "deactivating", "maintenance",
})
STANDARD_OUTPUTS = frozenset({
    "inherit", "null", "tty", "journal", "kmsg", "journal+console", "kmsg+console", "socket",
})
SLOT_STATUSES = ("OK", "UNUSED", "MISSING", "LINK", "NOT_REGULAR", "TOO_LARGE", "CHANGED", "UNREADABLE")
FULL_KEYS = frozenset({
    "status", "format_ok", "head_ok", "active_state_ok", "standard_output_ok",
    "head", "active_state", "standard_output", "files",
})
SINGLETON_STATUSES = ("USAGE", "ROOT_INVALID", "INPUT_CAP", "INTERNAL_ERROR")
OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_BINARY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
    | getattr(os, "O_NOCTTY", 0)
    | getattr(os, "O_CLOEXEC", 0)
)

_HEX40 = re.compile(r"[0-9a-f]{40}")
_SEGMENT = re.compile(r"[A-Za-z0-9._-]{1,255}")
_KV_KEYS = ("ActiveState", "StandardOutput")
_INTERNAL_ERROR_LINE = b'{"status":"INTERNAL_ERROR"}\n'


def _slot(status, digest=None, size=None):
    return {"status": status, "sha256": digest, "size": size}


def _valid_rel(rel):
    if not isinstance(rel, str) or not 1 <= len(rel) <= PATH_BOUND:
        return False
    for segment in rel.split("/"):
        if _SEGMENT.fullmatch(segment) is None or segment in (".", ".."):
            return False
    return True


def parse_args(argv):
    if not 2 <= len(argv) <= 1 + len(SLOTS):
        return None
    root = argv[0]
    if not isinstance(root, str) or not 1 <= len(root) <= ROOT_BOUND or "\x00" in root:
        return None
    if not os.path.isabs(root):
        return None
    rels = list(argv[1:])
    seen = set()
    for rel in rels:
        if not _valid_rel(rel) or rel in seen:
            return None
        seen.add(rel)
    return root, rels


def resolve_root(root):
    try:
        real = os.path.realpath(root, strict=True)
    except OSError:
        return None
    if not os.path.isdir(real):
        return None
    return real


def validate_metadata(data):
    result = {
        "format_ok": False, "head_ok": False, "active_state_ok": False, "standard_output_ok": False,
        "head": None, "active_state": None, "standard_output": None,
    }
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return result
    if not text.endswith("\n"):
        return result
    lines = text[:-1].split("\n")
    if len(lines) != 3:
        return result
    values = {}
    for line in lines[1:]:
        key, separator, value = line.partition("=")
        if not separator or key not in _KV_KEYS or key in values or not value or "=" in value:
            return result
        values[key] = value
    if len(values) != len(_KV_KEYS):
        return result
    result["format_ok"] = True
    head = lines[0]
    if _HEX40.fullmatch(head) is not None:
        result["head_ok"] = True
        result["head"] = head
    if values["ActiveState"] in ACTIVE_STATES:
        result["active_state_ok"] = True
        result["active_state"] = values["ActiveState"]
    if values["StandardOutput"] in STANDARD_OUTPUTS:
        result["standard_output_ok"] = True
        result["standard_output"] = values["StandardOutput"]
    return result


def _hash_open(fd, pre):
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        return _slot("NOT_REGULAR")
    if st.st_size > FILE_CAP:
        return _slot("TOO_LARGE")
    if (st.st_dev, st.st_ino) != (pre.st_dev, pre.st_ino):
        return _slot("CHANGED")
    digest = hashlib.sha256()
    limit = FILE_CAP + 1
    total = 0
    iterations = 0
    while total < limit:
        if iterations >= READ_ITERATIONS:
            return _slot("UNREADABLE")
        iterations += 1
        chunk = os.read(fd, min(CHUNK, limit - total))
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    if total > FILE_CAP:
        return _slot("TOO_LARGE")
    if total != st.st_size:
        return _slot("CHANGED")
    post = os.fstat(fd)
    if (post.st_dev, post.st_ino, post.st_size, post.st_mtime_ns) != (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns):
        return _slot("CHANGED")
    return _slot("OK", digest.hexdigest(), total)


def hash_slot(root_real, rel):
    """Containment, identity and bounded hashing for one relative path."""
    full = os.path.join(root_real, *rel.split("/"))
    try:
        real = os.path.realpath(full, strict=True)
    except FileNotFoundError:
        return _slot("MISSING")
    except OSError:
        return _slot("UNREADABLE")
    if real != full:
        return _slot("LINK")
    try:
        pre = os.lstat(full)
    except FileNotFoundError:
        return _slot("MISSING")
    except OSError:
        return _slot("UNREADABLE")
    if stat.S_ISLNK(pre.st_mode):
        return _slot("LINK")
    if not stat.S_ISREG(pre.st_mode):
        return _slot("NOT_REGULAR")
    if pre.st_size > FILE_CAP:
        return _slot("TOO_LARGE")
    try:
        fd = os.open(full, OPEN_FLAGS)
    except FileNotFoundError:
        return _slot("MISSING")
    except IsADirectoryError:
        return _slot("NOT_REGULAR")
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            return _slot("LINK")
        return _slot("UNREADABLE")
    try:
        return _hash_open(fd, pre)
    finally:
        os.close(fd)


def check(root_real, rels, metadata):
    result = {"status": "FAILED"}
    result.update(validate_metadata(metadata))
    files = {}
    for index, name in enumerate(SLOTS):
        files[name] = hash_slot(root_real, rels[index]) if index < len(rels) else _slot("UNUSED")
    result["files"] = files
    flags_ok = all(result[flag] for flag in ("format_ok", "head_ok", "active_state_ok", "standard_output_ok"))
    slots_ok = all(files[name]["status"] == "OK" for name in SLOTS[:len(rels)])
    if flags_ok and slots_ok:
        result["status"] = "OK"
    return result


def render(result):
    line = (json.dumps(result, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(line) > LINE_BOUND:
        raise ValueError("line bound exceeded")
    return line


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
        parsed = parse_args(argv)
        if parsed is None:
            line = render({"status": "USAGE"})
        else:
            root_real = resolve_root(parsed[0])
            if root_real is None:
                line = render({"status": "ROOT_INVALID"})
            else:
                if stdin is None:
                    stdin = sys.stdin.buffer
                metadata = _read(stdin, INPUT_CAP)
                if len(metadata) > INPUT_CAP:
                    line = render({"status": "INPUT_CAP"})
                else:
                    line = render(check(root_real, parsed[1], metadata))
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
