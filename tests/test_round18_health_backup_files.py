"""Round 18: /health surfaces actual backup file evidence.

R14 added heartbeat-age fields to /health. Those track the LAST RUN of
the rotate/create script — but a silently-deleted .bak file (operator
cleanup, disk-full truncate, accidental rm) leaves heartbeats fresh
while no backup exists. Round 18 adds file-evidence fields:

  backup_file_count       int   files matching scout.db.bak.* / .bak-*
  latest_backup_age_sec   int|None  mtime delta from now
  latest_backup_fresh     bool  age <= GECKO_BACKUP_STALE_AFTER_SEC
  latest_backup_size_bytes int|None
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.api import create_app


@pytest.fixture
def _scout_db_stub(tmp_path):
    import sqlite3

    db_path = tmp_path / "scout.db"
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE candidates (first_seen_at TEXT)")
    conn.commit()
    conn.close()
    return db_path


def _make_bak(p: Path, age_seconds: int, size_bytes: int = 1024) -> Path:
    p.write_bytes(b"x" * size_bytes)
    mtime = time.time() - age_seconds
    import os

    os.utime(p, (mtime, mtime))
    return p


def test_health_reports_zero_when_no_backups(monkeypatch, tmp_path, _scout_db_stub):
    monkeypatch.setenv("GECKO_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv(
        "GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(tmp_path / "missing2")
    )

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["backup_file_count"] == 0
    assert data["latest_backup_age_sec"] is None
    assert data["latest_backup_fresh"] is False
    assert data["latest_backup_size_bytes"] is None


def test_health_counts_backup_files(monkeypatch, tmp_path, _scout_db_stub):
    _make_bak(tmp_path / "scout.db.bak.20260524T231332Z", age_seconds=14400)
    _make_bak(tmp_path / "scout.db.bak.20260525T030000Z", age_seconds=3600)
    _make_bak(tmp_path / "scout.db.bak.20260525T060000Z", age_seconds=1800, size_bytes=2048)

    monkeypatch.setenv("GECKO_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv(
        "GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(tmp_path / "missing2")
    )

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["backup_file_count"] == 3
    # newest (1800s old) wins
    assert 1700 < data["latest_backup_age_sec"] < 2000
    assert data["latest_backup_fresh"] is True
    assert data["latest_backup_size_bytes"] == 2048


def test_health_flags_stale_latest_backup(monkeypatch, tmp_path, _scout_db_stub):
    """3-day-old backup → latest_backup_fresh=False even if files exist."""
    _make_bak(tmp_path / "scout.db.bak.20260522T030000Z", age_seconds=3 * 86400)

    monkeypatch.setenv("GECKO_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv(
        "GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(tmp_path / "missing2")
    )

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["backup_file_count"] == 1
    assert data["latest_backup_fresh"] is False


def test_health_handles_missing_backup_dir(monkeypatch, tmp_path, _scout_db_stub):
    monkeypatch.setenv("GECKO_BACKUP_DIR", str(tmp_path / "does-not-exist"))
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv(
        "GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(tmp_path / "missing2")
    )

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    # Missing dir → defaults preserved, no exception leaked
    assert data["backup_file_count"] == 0
    assert data["latest_backup_age_sec"] is None


def test_health_matches_both_naming_patterns(monkeypatch, tmp_path, _scout_db_stub):
    """The rotate script's glob accepts BOTH `scout.db.bak.*` and
    `scout.db.bak-*` (legacy hyphen variant). /health must too."""
    _make_bak(tmp_path / "scout.db.bak.20260525T030000Z", age_seconds=3600)
    _make_bak(tmp_path / "scout.db.bak-legacy-format", age_seconds=7200)

    monkeypatch.setenv("GECKO_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv(
        "GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(tmp_path / "missing2")
    )

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["backup_file_count"] == 2


def test_health_silently_deleted_backup_is_detectable(
    monkeypatch, tmp_path, _scout_db_stub
):
    """The R18 pathology: heartbeats are fresh (rotate + create script
    ran successfully recently) but a backup file was silently deleted.
    file_count=0 + fresh heartbeats → operator sees the mismatch."""
    rotate_hb = tmp_path / "rotate-hb"
    create_hb = tmp_path / "create-hb"
    rotate_hb.write_text(str(int(time.time() - 3600)))
    create_hb.write_text(str(int(time.time() - 3600)))
    monkeypatch.setenv("GECKO_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(rotate_hb))
    monkeypatch.setenv("GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(create_hb))

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    # Heartbeats fresh
    assert data["rotate_heartbeat_fresh"] is True
    assert data["create_heartbeat_fresh"] is True
    # But no actual backup file
    assert data["backup_file_count"] == 0, (
        "if file_count=0 while heartbeats are fresh, someone deleted "
        "the backup since the last script run — operator must know"
    )


def test_health_ignores_partial_artifacts_when_counting_and_dating(
    monkeypatch, tmp_path, _scout_db_stub
):
    """*** THE /health HALF OF THE 2026-08-08 INCIDENT. ***

    /health sorts newest-first and reports the top file's age as
    `latest_backup_age_sec`. A failed run's sidecar is always newer than the
    backup it failed to become, so counting them made backup health look FRESH
    while the newest completed backup was a week stale — the precise condition
    under which the operator would stop worrying.
    """
    _make_bak(tmp_path / "scout.db.bak.20260801T030001Z", age_seconds=7 * 86400)
    for suffix in (".partial", ".partial-journal", ".partial-wal", ".partial-shm"):
        _make_bak(
            tmp_path / f"scout.db.bak.20260808T030002Z{suffix}",
            age_seconds=60,
            size_bytes=1024,
        )

    monkeypatch.setenv("GECKO_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv(
        "GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(tmp_path / "missing2")
    )

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        data = client.get("/health").json()

    assert data["backup_file_count"] == 1, (
        "in-progress artifacts must not be counted as backups; "
        f"got {data['backup_file_count']}"
    )
    assert data["latest_backup_age_sec"] > 6 * 86400, (
        "age must reflect the newest COMPLETED backup (7d), not a 60s-old "
        f"sidecar; got {data['latest_backup_age_sec']}s"
    )
    assert data["latest_backup_fresh"] is False, (
        "a week-stale backup must not report fresh because a sidecar is new"
    )


def test_health_still_counts_manual_and_legacy_backup_names(
    monkeypatch, tmp_path, _scout_db_stub
):
    """The exclusion is `.partial*` only — the operator's manual-tag and legacy
    hyphen forms remain valid backups."""
    _make_bak(tmp_path / "scout.db.bak.manual-preupgrade", age_seconds=3600)
    _make_bak(tmp_path / "scout.db.bak-legacy-format", age_seconds=7200)
    _make_bak(tmp_path / "scout.db.bak.20260808T030002Z", age_seconds=1800)

    monkeypatch.setenv("GECKO_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv(
        "GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(tmp_path / "missing2")
    )

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        data = client.get("/health").json()

    assert data["backup_file_count"] == 3, data


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_health_ignores_sidecars_beside_a_completed_backup(
    monkeypatch, tmp_path, _scout_db_stub, suffix
):
    """*** THE /health HALF OF THE 2026-08-15 INCIDENT. ***

    A backup carries the live DB's WAL-mode header, so merely OPENING one makes
    SQLite mint `-wal` and `-shm` beside it — read-only opens included. Those
    names contain no `.partial`, so the exclusion added for the 2026-08-08
    family never covered them.

    This reader sorts by mtime and reports the newest file's age and size as THE
    BACKUP's. A 0-byte `-wal` minted by an integrity check is always newer than
    the backup it describes, so /health would report a fresh 0-byte backup while
    the real newest one aged out — and the operator's dashboard would look
    healthiest at exactly the moment it was least true.

    Rotation was fixed on 2026-08-15 and the off-host shipper alongside this
    test; this reader was the last one still counting them.
    """
    monkeypatch.setenv("GECKO_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(tmp_path / "missing1"))
    monkeypatch.setenv(
        "GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(tmp_path / "missing2")
    )

    real = tmp_path / "scout.db.bak.20260815T030000Z"
    real.write_bytes(b"x" * 4096)
    old = time.time() - 7200
    os.utime(real, (old, old))

    sidecar = tmp_path / ("scout.db.bak.20260815T030000Z" + suffix)
    sidecar.write_bytes(b"")
    fresh = time.time() - 30
    os.utime(sidecar, (fresh, fresh))

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()

    assert data["backup_file_count"] == 1, (
        f"a {suffix} sidecar was counted as a backup file"
    )
    assert data["latest_backup_size_bytes"] == 4096, (
        f"/health reported the {suffix} sidecar's size as the backup's"
    )
    assert data["latest_backup_age_sec"] >= 7200, (
        f"/health reported the {suffix} sidecar's age as the backup's, so a "
        "week-old backup would render as minutes fresh"
    )


def test_health_still_counts_a_tag_containing_a_sidecar_word(
    monkeypatch, tmp_path, _scout_db_stub
):
    """The exclusions anchor to the END of the name, so the supported ad-hoc-tag
    workflow is not narrowed — same deliberate choice as rotation and the
    shipper."""
    monkeypatch.setenv("GECKO_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(tmp_path / "missing1"))
    monkeypatch.setenv(
        "GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(tmp_path / "missing2")
    )

    tagged = tmp_path / "scout.db.bak.before-wal-migration"
    tagged.write_bytes(b"y" * 2048)

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()

    assert data["backup_file_count"] == 1
    assert data["latest_backup_size_bytes"] == 2048
