"""Regression test for the rollback-observability sweep.

Before this fix, scout/db.py had 12 sites where the inner cleanup ROLLBACK
in a migration error handler swallowed exceptions with ``except Exception:
pass``. That hid disk-failure / lock-timeout / corrupt-WAL events from
operators — the original migration error did get logged, but the rollback
itself failing left no audit trail.

This test grep-asserts that no ``except Exception: pass`` block follows a
ROLLBACK or ``self._conn.rollback()`` line. It's a static guard — running
the migrations to trigger rollback failures requires more setup than the
guard is worth, and the structural assertion is what matters: the
silent-swallow pattern must not regress.
"""

from __future__ import annotations

import re
from pathlib import Path


DB_PY = Path(__file__).resolve().parent.parent / "scout" / "db.py"


def test_no_silent_rollback_swallow_in_db_module():
    src = DB_PY.read_text(encoding="utf-8")

    # Match either pattern:
    #   try: ... ROLLBACK ... except Exception: pass
    #   try: ... self._conn.rollback() ... except Exception: pass
    bad_patterns = [
        re.compile(
            r'try:\s*\n\s+await conn\.execute\("ROLLBACK"\)\s*\n\s+except Exception:\s*\n\s+pass\b',
            re.MULTILINE,
        ),
        re.compile(
            r'try:\s*\n\s+await self\._conn\.rollback\(\)\s*\n\s+except Exception:\s*\n\s+pass\b',
            re.MULTILINE,
        ),
    ]

    findings = []
    for pat in bad_patterns:
        for m in pat.finditer(src):
            # Convert byte offset to line number for reportability.
            line = src.count("\n", 0, m.start()) + 1
            findings.append((line, m.group(0)[:80]))

    assert not findings, (
        "scout/db.py reintroduced silent rollback-swallow at: "
        + "; ".join(f"line {ln}" for ln, _ in findings)
        + ". Each rollback failure MUST log via _log/_db_log.exception so "
        "operators can see disk/lock/WAL failures during migrations."
    )


def test_every_rollback_cleanup_logs_on_failure():
    """All 12 rollback-cleanup blocks must log the inner failure."""
    src = DB_PY.read_text(encoding="utf-8")

    # Find every rollback line then check the immediately-following except
    # clause has a log call before any other statement.
    rollback_re = re.compile(
        r'(try:\s*\n\s+await (?:conn\.execute\("ROLLBACK"\)|self\._conn\.rollback\(\))\s*\n\s+except Exception(?: as \w+)?:\s*\n)(\s+)([^\n]+)',
        re.MULTILINE,
    )

    rollback_sites = list(rollback_re.finditer(src))
    assert (
        len(rollback_sites) >= 10
    ), f"expected >=10 rollback cleanup blocks, found {len(rollback_sites)}"

    for m in rollback_sites:
        line = src.count("\n", 0, m.start()) + 1
        first_stmt = m.group(3)
        assert (
            "_log.exception" in first_stmt or "_db_log.exception" in first_stmt
        ), (
            f"rollback cleanup at line {line} must log via "
            f"_log.exception / _db_log.exception as first statement; "
            f"got: {first_stmt!r}"
        )
