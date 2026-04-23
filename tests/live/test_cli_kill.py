"""Tests for the manual kill-switch CLI (scout/live/cli_kill.py).

The CLI resolves the DB path from the DB_PATH env var, opens the Database,
and drives KillSwitch.trigger / clear / is_active via --on/--off/--status.
"""

from __future__ import annotations

import sys
from datetime import timedelta

from scout.db import Database
from scout.live.cli_kill import main as cli_main
from scout.live.kill_switch import KillSwitch


async def test_on_triggers_kill(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    await db.initialize()
    await db.close()
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setattr(sys, "argv", ["cli_kill", "--on", "ops test"])
    await cli_main()
    db2 = Database(db_path)
    await db2.initialize()
    cur = await db2._conn.execute(
        "SELECT triggered_by, reason FROM kill_events ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    assert row[0] == "manual"
    assert row[1] == "ops test"
    await db2.close()


async def test_off_clears_active_kill(tmp_path, monkeypatch):
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    await db.initialize()
    ks = KillSwitch(db)
    await ks.trigger(
        triggered_by="manual",
        reason="pre-existing",
        duration=timedelta(hours=4),
    )
    assert await ks.is_active() is not None
    await db.close()

    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setattr(sys, "argv", ["cli_kill", "--off"])
    await cli_main()

    db2 = Database(db_path)
    await db2.initialize()
    ks2 = KillSwitch(db2)
    assert await ks2.is_active() is None
    cur = await db2._conn.execute(
        "SELECT cleared_by FROM kill_events ORDER BY id DESC LIMIT 1"
    )
    row = await cur.fetchone()
    assert row[0] == "manual"
    await db2.close()


async def test_status_when_inactive_and_active(tmp_path, monkeypatch, capsys):
    db_path = tmp_path / "t.db"
    db = Database(db_path)
    await db.initialize()
    await db.close()

    # Inactive state
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setattr(sys, "argv", ["cli_kill", "--status"])
    await cli_main()
    out_inactive = capsys.readouterr().out
    assert "inactive" in out_inactive

    # Trigger a kill, then check status again
    db = Database(db_path)
    await db.initialize()
    ks = KillSwitch(db)
    await ks.trigger(
        triggered_by="manual",
        reason="status-check",
        duration=timedelta(hours=4),
    )
    await db.close()

    monkeypatch.setattr(sys, "argv", ["cli_kill", "--status"])
    await cli_main()
    out_active = capsys.readouterr().out
    assert "active" in out_active
    assert "status-check" in out_active
    assert out_active != out_inactive
