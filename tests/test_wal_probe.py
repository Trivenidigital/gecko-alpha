"""Tests for Database.probe_wal_state() — BL-NEW-SQLITE-WAL-PROFILE cycle 4."""

from pathlib import Path

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "wal_test.db"))
    await database.initialize()
    yield database
    await database.close()


async def test_probe_wal_state_returns_required_fields(db):
    state = await db.probe_wal_state()
    # V20 SHOULD-FIX: explicit type assertions catch future PRAGMA driver
    # changes returning None instead of int.
    assert isinstance(state["wal_size_bytes"], int)
    assert isinstance(state["wal_pages"], int)
    assert isinstance(state["shm_size_bytes"], int)
    assert isinstance(state["db_size_bytes"], int)
    assert isinstance(state["page_count"], int)
    assert isinstance(state["page_size"], int)
    assert isinstance(state["freelist_count"], int)
    assert isinstance(state["wal_autocheckpoint"], int)
    # V20 MUST-FIX #2: defensive lowercase compare; catches silent WAL-mode
    # rejection on FS without shared-memory support.
    assert state["journal_mode"] == "wal", (
        f"journal_mode={state['journal_mode']!r} — WAL mode silently rejected? "
        f"PRAGMA journal_mode=WAL is set in Database.initialize()"
    )
    assert state["wal_size_bytes"] >= 0
    assert state["shm_size_bytes"] >= 0
    assert state["page_count"] > 0  # tables exist post-initialize


async def test_probe_wal_state_after_writes(db, token_factory):
    initial = await db.probe_wal_state()

    for i in range(100):
        token = token_factory(contract_address=f"0xtest_{i}", quant_score=50.0)
        await db.upsert_candidate(token)

    after = await db.probe_wal_state()
    assert after["db_size_bytes"] >= initial["db_size_bytes"]


async def test_probe_wal_state_wal_file_size_matches_stat(db):
    """If a .db-wal sidecar exists, its size should equal wal_size_bytes."""
    state = await db.probe_wal_state()
    wal_path = Path(db._db_path + "-wal")
    if wal_path.exists():
        assert state["wal_size_bytes"] == wal_path.stat().st_size
    else:
        assert state["wal_size_bytes"] == 0


async def test_probe_wal_state_wal_file_missing_returns_zero(db, monkeypatch):
    """V23 SHOULD-FIX: directly test the os.path.exists() = False branch.

    Mock `os.path.exists` to return False for the WAL path; verify probe
    returns wal_size_bytes=0 + wal_pages=0 without raising. Uses mocking
    rather than file-deletion because Windows can't unlink files held open
    by SQLite (works on Linux but flaky cross-platform).
    """
    import os as _os

    real_exists = _os.path.exists
    wal_path = db._db_path + "-wal"
    shm_path = db._db_path + "-shm"

    def _no_wal_exists(p):
        if p == wal_path:
            return False
        return real_exists(p)

    monkeypatch.setattr("os.path.exists", _no_wal_exists)

    state = await db.probe_wal_state()
    assert state["wal_size_bytes"] == 0
    assert state["wal_pages"] == 0
    # SHM path still exists (shm sidecar still on disk); only WAL is "missing"
    assert state["shm_size_bytes"] == (
        _os.path.getsize(shm_path) if real_exists(shm_path) else 0
    )
