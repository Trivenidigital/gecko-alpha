"""Tests for scripts/wal_archive.sh (REC-05b loud-failure fix).

Test methodology: run the bash script via subprocess with a fake `journalctl`
on PATH (PATH shim) and an isolated WAL_ARCHIVE_DIR. The regression under test:
before the fix, a `journalctl -p debug` that returned no matching
sqlite_wal_probe events (DEBUG-level events rotated out of journald) produced a
valid-but-empty gzip archive that decompressed to 0 bytes — a silent "captured
nothing" (0-byte-log-since-2026-05-31). The script must now either archive real
content OR fail LOUDLY (non-zero exit + stderr), never silent 0-byte success.

Skipped on Windows: bash + PATH-shim + gzip semantics are Linux-specific; the
script is deployed to Linux only (Hetzner VPS via root crontab).
"""

from __future__ import annotations

import gzip
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="bash + PATH-shim + gzip semantics are Linux-specific",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "wal_archive.sh"

_PROBE_LINE = (
    "Jul 11 03:45:01 srilu gecko-pipeline[1]: "
    '{"event": "sqlite_wal_probe", "wal_size_bytes": 14811136, '
    '"timestamp": "2026-07-11T03:45:01.123456Z"}'
)
_NONMATCHING_LINE = (
    "Jul 11 03:45:01 srilu gecko-pipeline[1]: "
    '{"event": "some_other_event", "timestamp": "2026-07-11T03:45:01.123456Z"}'
)


def _make_journalctl_stub(tmp_path: Path, output: str) -> Path:
    """Write a fake `journalctl` that ignores its args and prints `output`."""
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir(exist_ok=True)
    stub = stub_dir / "journalctl"
    payload = tmp_path / "journal_payload.txt"
    payload.write_text(output, encoding="utf-8")
    stub.write_text(
        "#!/usr/bin/env bash\n" f"cat {payload.as_posix()!r}\n" "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub_dir


def _run(tmp_path: Path, journal_output: str):
    stub_dir = _make_journalctl_stub(tmp_path, journal_output)
    archive_dir = tmp_path / "archive"
    env = os.environ.copy()
    env["PATH"] = f"{stub_dir}{os.pathsep}" + env.get("PATH", "")
    env["WAL_ARCHIVE_DIR"] = str(archive_dir)
    res = subprocess.run(
        ["bash", str(SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
    )
    return res, archive_dir


def _archives(archive_dir: Path) -> list[Path]:
    return sorted(archive_dir.glob("*.jsonl.gz"))


def test_archives_real_content_when_events_present(tmp_path):
    res, archive_dir = _run(tmp_path, _PROBE_LINE + "\n")
    assert res.returncode == 0, res.stderr
    archives = _archives(archive_dir)
    assert len(archives) == 1, f"expected one archive, got {archives}"
    content = gzip.decompress(archives[0].read_bytes()).decode("utf-8")
    assert content.strip(), "archive must not be empty"
    assert "sqlite_wal_probe" in content


def test_loud_failure_when_no_events(tmp_path):
    """Empty journal → non-zero exit + stderr diagnosis, NO empty archive."""
    res, archive_dir = _run(tmp_path, "")
    assert res.returncode != 0
    assert "no sqlite_wal_probe events" in res.stderr
    assert "Refusing to write an empty archive" in res.stderr
    assert _archives(archive_dir) == [], "must not leave a 0-byte archive"


def test_loud_failure_when_only_nonmatching_events(tmp_path):
    """journalctl returns lines, but none match the probe events → loud fail."""
    res, archive_dir = _run(tmp_path, _NONMATCHING_LINE + "\n")
    assert res.returncode != 0
    assert "no sqlite_wal_probe events" in res.stderr
    assert _archives(archive_dir) == []
