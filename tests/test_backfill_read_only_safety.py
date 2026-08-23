"""The backfill must not create WAL sidecars beside a file that is not live.

On 2026-08-15 a read-only open planted a 0-byte `-wal` next to the real
backups and the rotation then treated them as the newest backup, destroying
every genuine one. That bug had shipped in three separate readers.

`_open_read_only` guards against it by opening frozen files `immutable=1`,
which makes SQLite skip locking and ignore any `-wal` -- so no sidecar is
created. The live database is the deliberate exception: it has a running
writer, its sidecars already exist, and `immutable=1` there would hide
thousands of uncheckpointed rows.

The defect this pins: `live` was decided by comparing against the `--db`
ARGUMENT, so ANY target counted as live. Pointing the script at a backup --
the cautious thing `--dry-run` exists to allow -- opened it `mode=ro` and
planted the sidecars.
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backfill_chain_identity_recompute import (  # noqa: E402
    LIVE_DB,
    _is_live,
    _open_read_only,
)


def _make_wal_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE signal_events (token_id TEXT, created_at TEXT)")
    conn.execute("INSERT INTO signal_events VALUES ('t', '2026-08-01T00:00:00+00:00')")
    conn.commit()
    conn.close()


def test_a_backup_is_not_treated_as_live(tmp_path):
    backup = tmp_path / "scout.db.bak.20260815"
    _make_wal_db(backup)
    assert _is_live(str(backup)) is False
    assert _is_live(LIVE_DB) is True


def test_opening_a_backup_creates_no_sidecars(tmp_path):
    backup = tmp_path / "scout.db.bak.20260815"
    _make_wal_db(backup)
    # Prove the fixture is meaningful: a WAL database with no sidecars on disk
    # right now is exactly the state a rotated backup is in.
    for suffix in ("-wal", "-shm"):
        Path(str(backup) + suffix).unlink(missing_ok=True)

    conn = _open_read_only(str(backup), live=_is_live(str(backup)))
    conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()
    conn.close()

    created = [s for s in ("-wal", "-shm") if Path(str(backup) + s).exists()]
    assert created == [], (
        f"opening a backup created {created}; the rotation counts these as "
        "backups and they would displace the real ones"
    )


def test_the_sidecar_check_can_actually_fail(tmp_path):
    """A negative control. Without this the test above passes on any bug that
    stops sidecars being created at all -- including one that stops the file
    being opened."""
    backup = tmp_path / "other.db"
    _make_wal_db(backup)
    for suffix in ("-wal", "-shm"):
        Path(str(backup) + suffix).unlink(missing_ok=True)

    # `live=True` is the buggy classification; it must produce the sidecars.
    conn = _open_read_only(str(backup), live=True)
    conn.execute("SELECT COUNT(*) FROM signal_events").fetchone()
    conn.close()

    created = [s for s in ("-wal", "-shm") if Path(str(backup) + s).exists()]
    assert created, (
        "misclassifying a backup as live produced no sidecars, so the guard "
        "test above proves nothing on this platform"
    )
