"""Round 14: /health endpoint also surfaces backup heartbeat freshness.

Operators previously had to ssh + journalctl to verify R11/R13 backups
were producing. /health now reports the rotate AND create heartbeat
ages so any uptime monitor pointed at /health catches the same
conditions the gecko-backup-watchdog Telegram alert handles —
including the pre-R11 silent-failure shape (rotate fresh + create
stale).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dashboard.api import create_app


def _fresh_ts(age_seconds: int = 3600) -> str:
    return str(int(time.time() - age_seconds))


@pytest.fixture
def _scout_db_stub(tmp_path):
    """Empty scout.db file so create_app's _ro_db opens cleanly."""
    db_path = tmp_path / "scout.db"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE candidates (first_seen_at TEXT)"
    )
    conn.commit()
    conn.close()
    return db_path


def test_health_reports_both_heartbeats_fresh(monkeypatch, tmp_path, _scout_db_stub):
    rotate_hb = tmp_path / "rotate-hb"
    create_hb = tmp_path / "create-hb"
    rotate_hb.write_text(_fresh_ts(1800))  # 30min old
    create_hb.write_text(_fresh_ts(3600))  # 60min old
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(rotate_hb))
    monkeypatch.setenv("GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(create_hb))

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    data = r.json()
    assert data["rotate_heartbeat_fresh"] is True
    assert 1700 < data["rotate_heartbeat_age_sec"] < 2000
    assert data["create_heartbeat_fresh"] is True
    assert 3500 < data["create_heartbeat_age_sec"] < 3800


def test_health_flags_stale_rotate(monkeypatch, tmp_path, _scout_db_stub):
    rotate_hb = tmp_path / "rotate-hb"
    create_hb = tmp_path / "create-hb"
    rotate_hb.write_text(_fresh_ts(49 * 3600))  # 49h old — stale
    create_hb.write_text(_fresh_ts(3600))
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(rotate_hb))
    monkeypatch.setenv("GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(create_hb))

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["rotate_heartbeat_fresh"] is False
    assert data["create_heartbeat_fresh"] is True


def test_health_flags_stale_create_when_rotate_fresh(
    monkeypatch, tmp_path, _scout_db_stub
):
    """The pre-R11 silent-failure pathology — rotate runs against empty
    dir and stays fresh while create silently fails. /health must
    distinguish these so uptime monitors catch the gap."""
    rotate_hb = tmp_path / "rotate-hb"
    create_hb = tmp_path / "create-hb"
    rotate_hb.write_text(_fresh_ts(600))  # rotate 10min ago
    create_hb.write_text(_fresh_ts(72 * 3600))  # create 3d ago — stale
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(rotate_hb))
    monkeypatch.setenv("GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(create_hb))

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["rotate_heartbeat_fresh"] is True
    assert data["create_heartbeat_fresh"] is False, (
        "/health must flag create-heartbeat stale even when rotate is "
        "fresh — pre-R11 silent-failure shape"
    )


def test_health_missing_heartbeats_returns_none_and_false(
    monkeypatch, tmp_path, _scout_db_stub
):
    """Missing heartbeat files → age=None, fresh=False (so monitors alert)."""
    monkeypatch.setenv(
        "GECKO_BACKUP_HEARTBEAT_FILE", str(tmp_path / "no-such-rotate")
    )
    monkeypatch.setenv(
        "GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(tmp_path / "no-such-create")
    )

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["rotate_heartbeat_age_sec"] is None
    assert data["rotate_heartbeat_fresh"] is False
    assert data["create_heartbeat_age_sec"] is None
    assert data["create_heartbeat_fresh"] is False


def test_health_corrupt_heartbeat_returns_none_and_false(
    monkeypatch, tmp_path, _scout_db_stub
):
    """Corrupt (non-numeric) heartbeat → age=None, fresh=False."""
    hb = tmp_path / "rotate-hb"
    hb.write_text("not-a-number")
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(hb))
    monkeypatch.setenv(
        "GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(tmp_path / "missing")
    )

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["rotate_heartbeat_age_sec"] is None
    assert data["rotate_heartbeat_fresh"] is False


def test_health_threshold_via_env(monkeypatch, tmp_path, _scout_db_stub):
    """GECKO_BACKUP_STALE_AFTER_SEC tunes the freshness threshold."""
    hb = tmp_path / "rotate-hb"
    hb.write_text(_fresh_ts(1000))  # 1000s old
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(hb))
    monkeypatch.setenv(
        "GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(tmp_path / "missing")
    )

    # Threshold 500s → 1000s old should be STALE.
    monkeypatch.setenv("GECKO_BACKUP_STALE_AFTER_SEC", "500")
    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["rotate_heartbeat_fresh"] is False
