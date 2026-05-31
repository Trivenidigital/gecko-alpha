"""BL-NEW-TG-ALERT-OPERATOR-ACTION-TELEMETRY tests."""

from __future__ import annotations

import pytest

from scout.db import Database


@pytest.mark.asyncio
async def test_operator_action_migration_creates_table_and_indexes(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()

    cur = await db._conn.execute(
        "SELECT sql FROM sqlite_master "
        "WHERE type='table' AND name='tg_alert_operator_actions'"
    )
    table_sql = (await cur.fetchone())[0]
    assert "tg_alert_log_id INTEGER NOT NULL" in table_sql
    assert "REFERENCES tg_alert_log" not in table_sql
    assert "UNIQUE" in table_sql and "tg_alert_log_id" in table_sql
    for action in ("acted", "useful", "ignored", "false_positive"):
        assert action in table_sql

    cur = await db._conn.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type='index' AND name='idx_tg_alert_operator_actions_marked_at'"
    )
    assert await cur.fetchone()
    cur = await db._conn.execute(
        "SELECT 1 FROM paper_migrations "
        "WHERE name='bl_tg_alert_operator_actions_v1'"
    )
    assert await cur.fetchone()
    await db.close()


@pytest.mark.asyncio
async def test_operator_action_migration_marker_without_table_fails_loudly(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute("DROP TABLE tg_alert_operator_actions")
    await db._conn.commit()
    await db.close()

    db2 = Database(tmp_path / "t.db")
    with pytest.raises(RuntimeError, match="tg_alert_operator_actions.*missing"):
        await db2.initialize()
    await db2.close()


@pytest.mark.asyncio
async def test_operator_action_rows_survive_future_tg_alert_log_rebuild(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
        "VALUES (NULL, 'narrative_prediction', 'bonk', "
        "'2026-05-31T10:00:00+00:00', 'sent')"
    )
    alert_id = cur.lastrowid
    await db._conn.commit()
    await db.record_tg_alert_operator_action(
        tg_alert_log_id=alert_id,
        action="acted",
        note=None,
        source="dashboard",
    )

    await db._conn.execute(
        """CREATE TABLE tg_alert_log_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_trade_id INTEGER,
            signal_type TEXT NOT NULL,
            token_id TEXT NOT NULL,
            alerted_at TEXT NOT NULL,
            outcome TEXT NOT NULL,
            detail TEXT
        )"""
    )
    await db._conn.execute(
        "INSERT INTO tg_alert_log_new "
        "(id, paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
        "SELECT id, paper_trade_id, signal_type, token_id, alerted_at, outcome, detail "
        "FROM tg_alert_log"
    )
    await db._conn.execute("DROP TABLE tg_alert_log")
    await db._conn.execute("ALTER TABLE tg_alert_log_new RENAME TO tg_alert_log")
    await db._conn.commit()

    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_alert_operator_actions")
    assert (await cur.fetchone())[0] == 1
    await db.close()


@pytest.mark.asyncio
async def test_record_tg_alert_operator_action_upserts_current_label(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome, detail) "
        "VALUES (NULL, 'narrative_prediction', 'bonk', "
        "'2026-05-31T10:00:00+00:00', 'sent', 'delivered')"
    )
    alert_id = cur.lastrowid
    await db._conn.commit()

    first = await db.record_tg_alert_operator_action(
        tg_alert_log_id=alert_id,
        action="ignored",
        note="not useful today",
        source="dashboard",
    )
    assert first["action"] == "ignored"
    assert first["token_id"] == "bonk"
    assert first["signal_type"] == "narrative_prediction"

    second = await db.record_tg_alert_operator_action(
        tg_alert_log_id=alert_id,
        action="acted",
        note=None,
        source="dashboard",
    )
    assert second["id"] == first["id"]
    assert second["action"] == "acted"

    cur = await db._conn.execute("SELECT COUNT(*) FROM tg_alert_operator_actions")
    assert (await cur.fetchone())[0] == 1
    await db.close()


@pytest.mark.asyncio
async def test_missing_alert_does_not_rollback_unrelated_pending_write(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    await db._conn.execute(
        "CREATE TABLE rollback_probe (id INTEGER PRIMARY KEY, marker TEXT NOT NULL)"
    )
    await db._conn.commit()
    await db._conn.execute(
        "INSERT INTO rollback_probe (marker) VALUES ('pending before missing action')"
    )

    with pytest.raises(KeyError):
        await db.record_tg_alert_operator_action(
            tg_alert_log_id=999999,
            action="ignored",
            note=None,
            source="dashboard",
        )
    await db._conn.commit()

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM rollback_probe "
        "WHERE marker='pending before missing action'"
    )
    assert (await cur.fetchone())[0] == 1
    await db.close()


@pytest.mark.asyncio
async def test_record_tg_alert_operator_action_rejects_invalid_action(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    cur = await db._conn.execute(
        "INSERT INTO tg_alert_log "
        "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
        "VALUES (NULL, 'volume_spike', 'wif', "
        "'2026-05-31T10:00:00+00:00', 'sent')"
    )
    await db._conn.commit()

    with pytest.raises(ValueError, match="invalid operator action"):
        await db.record_tg_alert_operator_action(
            tg_alert_log_id=cur.lastrowid,
            action="buy_now",
            note=None,
            source="dashboard",
        )
    await db.close()
