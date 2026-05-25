"""Round 20: /health surfaces off-host backup status.

R11/R13/R18/R19 cover local backup creation, rotation, watchdog, file
evidence, and UI render. All of those still leave the failure mode
"VPS filesystem destroyed — backups gone with the live DB." R20 adds
off-host shipping via gecko-backup-offhost.sh and exposes its status
through /health so the operator can confirm transfers are landing.

Three /health fields:

  offhost_configured        bool  GECKO_OFFHOST_BACKUP_DEST is non-empty
  offhost_heartbeat_age_sec int|None  seconds since last successful ship
  offhost_heartbeat_fresh   bool  age <= GECKO_BACKUP_STALE_AFTER_SEC

Semantic: "configured" separates "operator hasn't enabled this" (false)
from "enabled but stale" (configured=true + fresh=false). A stale
heartbeat must NOT be silently rendered as fresh just because the env
isn't set; that would mask a regression where the env was unset by
accident.
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


def _set_local_backup_env(monkeypatch, tmp_path: Path) -> None:
    """Stable env so local backup checks don't bleed into off-host assertions."""
    monkeypatch.setenv("GECKO_BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("GECKO_BACKUP_HEARTBEAT_FILE", str(tmp_path / "missing1"))
    monkeypatch.setenv(
        "GECKO_BACKUP_CREATE_HEARTBEAT_FILE", str(tmp_path / "missing2")
    )


def test_health_offhost_disabled_when_dest_unset(
    monkeypatch, tmp_path, _scout_db_stub
):
    """Default state: off-host destination unconfigured."""
    _set_local_backup_env(monkeypatch, tmp_path)
    monkeypatch.delenv("GECKO_OFFHOST_BACKUP_DEST", raising=False)
    monkeypatch.setenv(
        "GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE", str(tmp_path / "missing-hb")
    )

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["offhost_configured"] is False
    assert data["offhost_heartbeat_age_sec"] is None
    assert data["offhost_heartbeat_fresh"] is False


def test_health_offhost_fresh_when_heartbeat_recent(
    monkeypatch, tmp_path, _scout_db_stub
):
    """Configured + heartbeat within stale window → fresh."""
    _set_local_backup_env(monkeypatch, tmp_path)
    hb = tmp_path / "offhost-hb"
    hb.write_text(str(int(time.time() - 3600)))  # 1h old
    monkeypatch.setenv("GECKO_OFFHOST_BACKUP_DEST", "user@host:/backups/")
    monkeypatch.setenv("GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE", str(hb))

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["offhost_configured"] is True
    assert data["offhost_heartbeat_fresh"] is True
    assert 3500 < data["offhost_heartbeat_age_sec"] < 3700


def test_health_offhost_stale_when_heartbeat_old(
    monkeypatch, tmp_path, _scout_db_stub
):
    """Configured but heartbeat older than stale window → stale."""
    _set_local_backup_env(monkeypatch, tmp_path)
    hb = tmp_path / "offhost-hb"
    hb.write_text(str(int(time.time() - 3 * 86400)))  # 3d old, > 48h default
    monkeypatch.setenv("GECKO_OFFHOST_BACKUP_DEST", "/mnt/external/backups/")
    monkeypatch.setenv("GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE", str(hb))

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["offhost_configured"] is True
    assert data["offhost_heartbeat_fresh"] is False
    assert data["offhost_heartbeat_age_sec"] > 2 * 86400


def test_health_offhost_configured_but_never_run_is_stale(
    monkeypatch, tmp_path, _scout_db_stub
):
    """Configured but heartbeat file doesn't exist (never succeeded yet).
    Must NOT render as fresh — operator needs to see the gap."""
    _set_local_backup_env(monkeypatch, tmp_path)
    monkeypatch.setenv("GECKO_OFFHOST_BACKUP_DEST", "user@host:/backups/")
    monkeypatch.setenv(
        "GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE", str(tmp_path / "never-written")
    )

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["offhost_configured"] is True
    assert data["offhost_heartbeat_age_sec"] is None
    assert data["offhost_heartbeat_fresh"] is False


def test_health_offhost_corrupt_heartbeat_treated_stale(
    monkeypatch, tmp_path, _scout_db_stub
):
    """Heartbeat content non-numeric (fs corruption, mid-write truncate) →
    age=None + fresh=False (don't crash trying to int() the garbage)."""
    _set_local_backup_env(monkeypatch, tmp_path)
    hb = tmp_path / "offhost-hb"
    hb.write_text("not-a-timestamp\n")
    monkeypatch.setenv("GECKO_OFFHOST_BACKUP_DEST", "user@host:/backups/")
    monkeypatch.setenv("GECKO_OFFHOST_BACKUP_HEARTBEAT_FILE", str(hb))

    app = create_app(str(_scout_db_stub))
    with TestClient(app) as client:
        r = client.get("/health")
    data = r.json()
    assert data["offhost_configured"] is True
    assert data["offhost_heartbeat_age_sec"] is None
    assert data["offhost_heartbeat_fresh"] is False
