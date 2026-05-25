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
