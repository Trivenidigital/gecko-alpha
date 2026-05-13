"""BL-NEW-MINARA-DB-PERSISTENCE tests."""

from __future__ import annotations

import asyncio

import pytest
import aiosqlite

from scout.db import Database
from scripts.backfill_minara_alert_emissions import (
    backfill_file,
    parse_minara_emission_line,
)
from scripts.check_minara_emission_persistence import check_persistence_parity


async def _insert_paper_trade(db: Database, *, trade_id: int = 42) -> None:
    if db._conn is None:
        raise RuntimeError("db not initialized")
    await db._conn.execute(
        """INSERT INTO paper_trades
           (id, token_id, symbol, name, chain, signal_type, signal_data,
            entry_price, amount_usd, quantity, tp_pct, sl_pct, tp_price,
            sl_price, status, opened_at)
           VALUES (?, ?, 'TST', 'Test', 'solana', 'gainers_early',
                   '{}', 100.0, 10.0, 0.1, 20.0, 10.0, 120.0, 90.0,
                   'open', '2026-05-13T00:00:00+00:00')""",
        (trade_id, f"coin-{trade_id}"),
    )
    await db._conn.commit()


@pytest.mark.asyncio
async def test_minara_alert_emissions_table_created(tmp_path):
    db = Database(tmp_path / "scout.db")
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='minara_alert_emissions'"
        )
        assert await cur.fetchone() is not None

        cur = await db._conn.execute("PRAGMA table_info(minara_alert_emissions)")
        cols = {row[1] for row in await cur.fetchall()}
        assert {
            "id",
            "paper_trade_id",
            "tg_alert_log_id",
            "signal_type",
            "coin_id",
            "chain",
            "amount_usd",
            "command_text",
            "command_hash",
            "command_text_observed",
            "source",
            "source_event_id",
            "emitted_at",
            "operator_paste_acknowledged_at",
        }.issubset(cols)
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_record_minara_alert_emission_inserts_live_context(tmp_path):
    db = Database(tmp_path / "scout.db")
    await db.initialize()
    try:
        await _insert_paper_trade(db, trade_id=42)
        cur = await db._conn.execute(
            "INSERT INTO tg_alert_log "
            "(paper_trade_id, signal_type, token_id, alerted_at, outcome) "
            "VALUES (?, ?, ?, ?, 'sent')",
            (42, "gainers_early", "goblincoin", "2026-05-13T00:00:01+00:00"),
        )
        tg_alert_log_id = cur.lastrowid
        await db._conn.commit()

        inserted = await db.record_minara_alert_emission(
            paper_trade_id=42,
            tg_alert_log_id=tg_alert_log_id,
            signal_type="gainers_early",
            coin_id="goblincoin",
            chain="solana",
            amount_usd=10,
            command_text="minara swap --from USDC --to ABC --amount-usd 10",
            emitted_at="2026-05-13T00:00:02+00:00",
        )
        assert inserted is True

        cur = await db._conn.execute(
            "SELECT paper_trade_id, tg_alert_log_id, signal_type, coin_id, "
            "chain, amount_usd, command_text, command_hash, "
            "command_text_observed, source, source_event_id "
            "FROM minara_alert_emissions"
        )
        row = await cur.fetchone()
        assert row["paper_trade_id"] == 42
        assert row["tg_alert_log_id"] == tg_alert_log_id
        assert row["signal_type"] == "gainers_early"
        assert row["coin_id"] == "goblincoin"
        assert row["chain"] == "solana"
        assert row["amount_usd"] == 10
        assert row["command_text"].startswith("minara swap")
        assert len(row["command_hash"]) == 64
        assert row["command_text_observed"] == 1
        assert row["source"] == "live"
        assert row["source_event_id"] == f"tg_alert_log:{tg_alert_log_id}"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_record_minara_alert_emission_duplicate_returns_false(tmp_path):
    db = Database(tmp_path / "scout.db")
    await db.initialize()
    try:
        first = await db.record_minara_alert_emission(
            paper_trade_id=None,
            tg_alert_log_id=None,
            signal_type="unknown_historical_backfill",
            coin_id="goblincoin",
            chain="solana",
            amount_usd=10,
            command_text=None,
            emitted_at="2026-05-11T22:26:10+00:00",
            source_event_id="journalctl:2026-05-11T22:26:10Z:goblincoin:solana:10",
            source="journalctl_backfill",
        )
        second = await db.record_minara_alert_emission(
            paper_trade_id=None,
            tg_alert_log_id=None,
            signal_type="unknown_historical_backfill",
            coin_id="goblincoin",
            chain="solana",
            amount_usd=10,
            command_text=None,
            emitted_at="2026-05-11T22:26:10+00:00",
            source_event_id="journalctl:2026-05-11T22:26:10Z:goblincoin:solana:10",
            source="journalctl_backfill",
        )
        assert first is True
        assert second is False
        cur = await db._conn.execute("SELECT COUNT(*) FROM minara_alert_emissions")
        assert (await cur.fetchone())[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_record_minara_alert_emission_invalid_source_raises(tmp_path):
    db = Database(tmp_path / "scout.db")
    await db.initialize()
    try:
        with pytest.raises(Exception):
            await db.record_minara_alert_emission(
                paper_trade_id=None,
                tg_alert_log_id=None,
                signal_type="unknown_historical_backfill",
                coin_id="goblincoin",
                chain="solana",
                amount_usd=10,
                command_text=None,
                source_event_id="journalctl:bad-source:goblincoin:solana:10",
                source="bogus",
            )
        cur = await db._conn.execute("SELECT COUNT(*) FROM minara_alert_emissions")
        assert (await cur.fetchone())[0] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_record_minara_alert_emission_requires_source_event_without_tg_id(
    tmp_path,
):
    db = Database(tmp_path / "scout.db")
    await db.initialize()
    try:
        with pytest.raises(ValueError, match="source_event_id"):
            await db.record_minara_alert_emission(
                paper_trade_id=None,
                tg_alert_log_id=None,
                signal_type="unknown_historical_backfill",
                coin_id="goblincoin",
                chain="solana",
                amount_usd=10,
                command_text=None,
            )
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_record_minara_alert_emission_lock_timeout_before_write(tmp_path):
    db = Database(tmp_path / "scout.db")
    await db.initialize()
    await db._txn_lock.acquire()
    try:
        with pytest.raises(asyncio.TimeoutError):
            await db.record_minara_alert_emission(
                paper_trade_id=None,
                tg_alert_log_id=None,
                signal_type="unknown_historical_backfill",
                coin_id="goblincoin",
                chain="solana",
                amount_usd=10,
                command_text=None,
                source_event_id="journalctl:locked:goblincoin:solana:10",
                source="journalctl_backfill",
                lock_timeout_sec=0.001,
            )
    finally:
        db._txn_lock.release()
        cur = await db._conn.execute("SELECT COUNT(*) FROM minara_alert_emissions")
        assert (await cur.fetchone())[0] == 0
        await db.close()


@pytest.mark.asyncio
async def test_minara_alert_emissions_migration_stamped_and_idempotent(tmp_path):
    db = Database(tmp_path / "scout.db")
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT version, description FROM schema_version WHERE version = ?",
            (20260519,),
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["description"] == "bl_minara_alert_emissions_v1"

        cur = await db._conn.execute(
            "SELECT name FROM paper_migrations WHERE name = ?",
            ("bl_minara_alert_emissions_v1",),
        )
        assert await cur.fetchone() is not None

        await db._migrate_minara_alert_emissions_v1()
        cur = await db._conn.execute(
            "SELECT COUNT(*) FROM paper_migrations WHERE name = ?",
            ("bl_minara_alert_emissions_v1",),
        )
        assert (await cur.fetchone())[0] == 1
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_minara_alert_emissions_migration_rejects_partial_table(tmp_path):
    db_path = tmp_path / "scout.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE minara_alert_emissions (id INTEGER PRIMARY KEY)"
        )
        await conn.commit()

    db = Database(db_path)
    with pytest.raises(RuntimeError, match="schema missing columns"):
        await db.initialize()
    await db.close()


@pytest.mark.asyncio
async def test_minara_alert_emissions_migration_ignores_stale_marker(tmp_path):
    db_path = tmp_path / "scout.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute(
            "CREATE TABLE paper_migrations "
            "(name TEXT PRIMARY KEY, cutover_ts TEXT NOT NULL)"
        )
        await conn.execute(
            "INSERT INTO paper_migrations (name, cutover_ts) VALUES (?, ?)",
            ("bl_minara_alert_emissions_v1", "2026-05-13T00:00:00Z"),
        )
        await conn.commit()

    db = Database(db_path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='minara_alert_emissions'"
        )
        assert await cur.fetchone() is not None
        cur = await db._conn.execute(
            "SELECT description FROM schema_version WHERE version = ?",
            (20260519,),
        )
        assert (await cur.fetchone())["description"] == "bl_minara_alert_emissions_v1"
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_minara_alert_emissions_migration_rejects_bad_constraints(tmp_path):
    db_path = tmp_path / "scout.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""CREATE TABLE minara_alert_emissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_trade_id INTEGER,
                tg_alert_log_id INTEGER,
                signal_type TEXT NOT NULL,
                coin_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                command_text TEXT,
                command_hash TEXT,
                command_text_observed INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL,
                source_event_id TEXT NOT NULL UNIQUE,
                emitted_at TEXT NOT NULL,
                operator_paste_acknowledged_at TEXT
            )""")
        await conn.execute(
            "CREATE UNIQUE INDEX idx_minara_alert_emissions_tg_alert_log_id "
            "ON minara_alert_emissions(tg_alert_log_id) "
            "WHERE tg_alert_log_id IS NOT NULL"
        )
        await conn.commit()

    db = Database(db_path)
    with pytest.raises(RuntimeError, match="CHECK|FK|constraint|missing"):
        await db.initialize()
    await db.close()


@pytest.mark.asyncio
async def test_minara_alert_emissions_migration_rejects_bad_defaults(tmp_path):
    db_path = tmp_path / "scout.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""CREATE TABLE minara_alert_emissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_trade_id INTEGER REFERENCES paper_trades(id) ON DELETE RESTRICT,
                tg_alert_log_id INTEGER,
                signal_type TEXT NOT NULL DEFAULT 'bad',
                coin_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                command_text TEXT,
                command_hash TEXT,
                command_text_observed INTEGER NOT NULL DEFAULT 0
                    CHECK (command_text_observed IN (0,1)),
                source TEXT NOT NULL
                    CHECK (source IN ('live','journalctl_backfill')),
                source_event_id TEXT NOT NULL UNIQUE,
                emitted_at TEXT NOT NULL,
                operator_paste_acknowledged_at TEXT
            )""")
        await conn.execute(
            "CREATE INDEX idx_minara_alert_emissions_emitted_at "
            "ON minara_alert_emissions(emitted_at)"
        )
        await conn.execute(
            "CREATE INDEX idx_minara_alert_emissions_coin_id "
            "ON minara_alert_emissions(coin_id, emitted_at)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX idx_minara_alert_emissions_tg_alert_log_id "
            "ON minara_alert_emissions(tg_alert_log_id) "
            "WHERE tg_alert_log_id IS NOT NULL"
        )
        await conn.commit()

    db = Database(db_path)
    with pytest.raises(RuntimeError, match="default mismatch"):
        await db.initialize()
    await db.close()


@pytest.mark.asyncio
async def test_minara_alert_emissions_migration_rejects_bad_index_shape(tmp_path):
    db_path = tmp_path / "scout.db"
    async with aiosqlite.connect(db_path) as conn:
        await conn.execute("""CREATE TABLE minara_alert_emissions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_trade_id INTEGER REFERENCES paper_trades(id) ON DELETE RESTRICT,
                tg_alert_log_id INTEGER,
                signal_type TEXT NOT NULL,
                coin_id TEXT NOT NULL,
                chain TEXT NOT NULL,
                amount_usd REAL NOT NULL,
                command_text TEXT,
                command_hash TEXT,
                command_text_observed INTEGER NOT NULL DEFAULT 0
                    CHECK (command_text_observed IN (0,1)),
                source TEXT NOT NULL
                    CHECK (source IN ('live','journalctl_backfill')),
                source_event_id TEXT NOT NULL UNIQUE,
                emitted_at TEXT NOT NULL,
                operator_paste_acknowledged_at TEXT
            )""")
        await conn.execute(
            "CREATE INDEX idx_minara_alert_emissions_emitted_at "
            "ON minara_alert_emissions(coin_id)"
        )
        await conn.execute(
            "CREATE INDEX idx_minara_alert_emissions_coin_id "
            "ON minara_alert_emissions(emitted_at)"
        )
        await conn.execute(
            "CREATE UNIQUE INDEX idx_minara_alert_emissions_tg_alert_log_id "
            "ON minara_alert_emissions(tg_alert_log_id) "
            "WHERE tg_alert_log_id IS NOT NULL"
        )
        await conn.commit()

    db = Database(db_path)
    with pytest.raises(RuntimeError, match="columns mismatch"):
        await db.initialize()
    await db.close()


@pytest.mark.asyncio
async def test_minara_alert_emissions_migration_cancel_rolls_back(
    tmp_path, monkeypatch
):
    db = Database(tmp_path / "scout.db")

    async def _cancel_assert(*args, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(db, "_assert_minara_alert_emissions_schema", _cancel_assert)
    with pytest.raises(asyncio.CancelledError):
        await db.initialize()
    assert db._conn is not None
    assert db._conn.in_transaction is False
    await db.close()


def test_parse_minara_emission_json_line():
    line = (
        '{"event":"minara_alert_command_emitted","coin_id":"goblincoin",'
        '"chain":"solana","amount_usd":10,'
        '"timestamp":"2026-05-11T22:26:10Z"}'
    )
    row = parse_minara_emission_line(line)
    assert row == {
        "coin_id": "goblincoin",
        "chain": "solana",
        "amount_usd": 10,
        "emitted_at": "2026-05-11T22:26:10Z",
        "source_event_id": "journalctl:2026-05-11T22:26:10Z:goblincoin:solana:10",
    }


def test_parse_minara_emission_json_line_uses_logged_source_event_id():
    line = (
        '{"event":"minara_alert_command_emitted","coin_id":"goblincoin",'
        '"chain":"solana","amount_usd":10,'
        '"timestamp":"2026-05-11T22:26:10Z",'
        '"source_event_id":"tg_alert_log:123"}'
    )
    row = parse_minara_emission_line(line)
    assert row["source_event_id"] == "tg_alert_log:123"


@pytest.mark.asyncio
async def test_backfill_file_apply_is_idempotent(tmp_path):
    db_path = tmp_path / "scout.db"
    journal_path = tmp_path / "minara.jsonl"
    journal_path.write_text(
        "\n".join(
            [
                '{"event":"not_it","coin_id":"x"}',
                (
                    '{"event":"minara_alert_command_emitted",'
                    '"coin_id":"goblincoin","chain":"solana","amount_usd":10,'
                    '"timestamp":"2026-05-11T22:26:10Z"}'
                ),
            ]
        ),
        encoding="utf-8",
    )

    assert await backfill_file(db_path, journal_path, apply=False) == 1
    assert await backfill_file(db_path, journal_path, apply=True) == 1
    assert await backfill_file(db_path, journal_path, apply=True) == 0

    db = Database(db_path)
    await db.initialize()
    try:
        cur = await db._conn.execute(
            "SELECT source, command_text, command_hash, command_text_observed "
            "FROM minara_alert_emissions"
        )
        row = await cur.fetchone()
        assert row["source"] == "journalctl_backfill"
        assert row["command_text"] is None
        assert row["command_hash"] is None
        assert row["command_text_observed"] == 0
    finally:
        await db.close()


@pytest.mark.asyncio
async def test_minara_emission_persistence_watchdog_passes_when_db_has_rows(
    tmp_path,
):
    db_path = tmp_path / "scout.db"
    journal_path = tmp_path / "minara.jsonl"
    journal_path.write_text(
        "\n".join(
            [
                (
                    '{"event":"minara_alert_command_emitted",'
                    '"coin_id":"goblincoin","chain":"solana","amount_usd":10,'
                    '"timestamp":"2026-05-11T22:26:10Z"}'
                ),
                (
                    '{"event":"minara_alert_command_emitted",'
                    '"coin_id":"hanta","chain":"solana","amount_usd":10,'
                    '"timestamp":"2026-05-11T22:27:10Z"}'
                ),
            ]
        ),
        encoding="utf-8",
    )
    assert await backfill_file(db_path, journal_path, apply=True) == 2

    result = check_persistence_parity(db_path, journal_path)

    assert result["ok"] is True
    assert result["journal_count"] == 2
    assert result["db_count"] == 2
    assert result["deficit"] == 0


@pytest.mark.asyncio
async def test_minara_emission_persistence_watchdog_detects_missing_rows(
    tmp_path,
):
    db_path = tmp_path / "scout.db"
    journal_path = tmp_path / "minara.jsonl"
    journal_path.write_text(
        "\n".join(
            [
                (
                    '{"event":"minara_alert_command_emitted",'
                    '"coin_id":"goblincoin","chain":"solana","amount_usd":10,'
                    '"timestamp":"2026-05-11T22:26:10Z"}'
                ),
                (
                    '{"event":"minara_alert_command_emitted",'
                    '"coin_id":"hanta","chain":"solana","amount_usd":10,'
                    '"timestamp":"2026-05-11T22:27:10Z"}'
                ),
            ]
        ),
        encoding="utf-8",
    )
    db = Database(db_path)
    await db.initialize()
    try:
        await db.record_minara_alert_emission(
            paper_trade_id=None,
            tg_alert_log_id=None,
            signal_type="unknown_historical_backfill",
            coin_id="goblincoin",
            chain="solana",
            amount_usd=10,
            command_text=None,
            emitted_at="2026-05-11T22:26:10Z",
            source_event_id="journalctl:2026-05-11T22:26:10Z:goblincoin:solana:10",
            source="journalctl_backfill",
        )
    finally:
        await db.close()

    result = check_persistence_parity(db_path, journal_path)

    assert result["ok"] is False
    assert result["journal_count"] == 2
    assert result["db_count"] == 1
    assert result["deficit"] == 1
