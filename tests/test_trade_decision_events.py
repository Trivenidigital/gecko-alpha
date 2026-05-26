"""Trade dispatch decision event log coverage."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scout.config import Settings
from scout.db import Database
from scout.trading.engine import TradingEngine
from scout.trading.params import bump_cache_version
from scout.trading.signals import trade_gainers


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        DB_PATH=tmp_path / "test.db",
        TRADING_ENABLED=True,
        TRADING_MODE="paper",
        SIGNAL_PARAMS_ENABLED=True,
        PAPER_TRADE_AMOUNT_USD=1000.0,
        PAPER_MAX_EXPOSURE_USD=10_000.0,
        PAPER_TP_PCT=20.0,
        PAPER_SL_PCT=10.0,
        PAPER_SLIPPAGE_BPS=50,
        PAPER_MAX_DURATION_HOURS=48,
        PAPER_MIN_MCAP=5_000_000,
        PAPER_MAX_OPEN_TRADES=1000,
        PAPER_STARTUP_WARMUP_SECONDS=0,
    )


@pytest.fixture
def engine(db, settings):
    return TradingEngine(mode="paper", db=db, settings=settings)


async def _seed_price(db, coin_id: str, price: float = 1.0) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
           (coin_id, current_price, price_change_24h, price_change_7d, market_cap, updated_at)
           VALUES (?, ?, 0, 0, 10000000, ?)""",
        (coin_id, price, now),
    )
    await db._conn.commit()


async def _insert_gainer(
    db,
    coin_id: str,
    *,
    market_cap: float | None,
    price_change_24h: float = 25.0,
    price: float = 1.0,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
           (coin_id, symbol, name, price_change_24h, market_cap, volume_24h,
            price_at_snapshot, snapshot_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            coin_id,
            coin_id.upper(),
            coin_id,
            price_change_24h,
            market_cap,
            100_000.0,
            price,
            now,
        ),
    )
    await db._conn.commit()


async def _latest_decision(db, token_id: str):
    cur = await db._conn.execute(
        """SELECT * FROM trade_decision_events
           WHERE token_id = ?
           ORDER BY id DESC LIMIT 1""",
        (token_id,),
    )
    return await cur.fetchone()


def _event_data(row) -> dict:
    return json.loads(row["event_data"])


async def test_trade_decision_events_table_created(db):
    cur = await db._conn.execute("PRAGMA table_info(trade_decision_events)")
    columns = {row["name"] for row in await cur.fetchall()}
    assert {
        "id",
        "token_id",
        "signal_type",
        "decision",
        "reason",
        "source_module",
        "signal_combo",
        "paper_trade_id",
        "event_data",
        "created_at",
    }.issubset(columns)


async def test_trade_decision_events_schema_version_collision_raises(db):
    await db._conn.execute(
        "UPDATE schema_version SET description = ? WHERE version = ?",
        ("some_other_migration", 20260526),
    )
    await db._conn.commit()

    with pytest.raises(RuntimeError, match="description mismatch"):
        await db._migrate_trade_decision_events_v1()


async def test_engine_emits_opened_trade_decision_event(engine, db):
    await _seed_price(db, "bitcoin", 50_000.0)
    trade_id = await engine.open_trade(
        token_id="bitcoin",
        symbol="BTC",
        name="Bitcoin",
        chain="coingecko",
        signal_type="volume_spike",
        signal_data={"spike_ratio": 12.3},
        signal_combo="volume_spike",
    )

    row = await _latest_decision(db, "bitcoin")
    assert trade_id is not None
    assert row["decision"] == "opened"
    assert row["reason"] == "paper_trade_opened"
    assert row["paper_trade_id"] == trade_id
    assert _event_data(row)["signal_data"]["spike_ratio"] == 12.3


async def test_engine_emits_disabled_signal_decision_event(engine, db, settings):
    await db._conn.execute("""UPDATE signal_params
           SET enabled=0, suspended_reason='hard_loss', updated_by='test'
           WHERE signal_type='gainers_early'""")
    await db._conn.commit()
    bump_cache_version()

    trade_id = await engine.open_trade(
        token_id="toes",
        symbol="TOES",
        name="TOES",
        chain="coingecko",
        signal_type="gainers_early",
        signal_data={"mcap": 16_500_000, "price_change_24h": 29.4},
        entry_price=1.0,
        signal_combo="gainers_early",
    )

    row = await _latest_decision(db, "toes")
    assert trade_id is None
    assert row["decision"] == "blocked"
    assert row["reason"] == "signal_disabled"
    assert _event_data(row)["signal_params_source"] == "table"
    assert _event_data(row)["signal_data"]["mcap"] == 16_500_000


async def test_trade_gainers_emits_pre_engine_late_pump_event(db, engine, settings):
    settings.PAPER_GAINERS_MAX_24H_PCT = 50.0
    await _insert_gainer(
        db,
        "too-late",
        market_cap=25_000_000,
        price_change_24h=75.0,
    )

    await trade_gainers(engine, db, min_mcap=5_000_000, settings=settings)

    row = await _latest_decision(db, "too-late")
    assert row["decision"] == "blocked"
    assert row["reason"] == "late_pump"
    data = _event_data(row)
    assert data["market_cap"] == 25_000_000
    assert data["price_change_24h"] == 75.0
    assert data["max_24h_pct"] == 50.0


async def test_emit_trade_decision_fail_soft_when_db_closed(tmp_path):
    from scout.trading.decision_events import emit_trade_decision

    d = Database(tmp_path / "closed.db")
    result = await emit_trade_decision(
        d,
        token_id="x",
        signal_type="gainers_early",
        decision="blocked",
        reason="db_closed",
        source_module="test",
    )
    assert result is None


async def test_emit_trade_decision_uses_txn_lock(db, monkeypatch):
    from scout.trading.decision_events import emit_trade_decision

    class ExplodingLock:
        async def __aenter__(self):
            raise RuntimeError("lock entered")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(db, "_txn_lock", ExplodingLock())

    result = await emit_trade_decision(
        db,
        token_id="x",
        signal_type="gainers_early",
        decision="blocked",
        reason="test",
        source_module="test",
    )
    assert result is None


def test_trade_decision_event_checker_fails_when_tracker_rows_have_no_decisions(
    tmp_path,
):
    db_path = tmp_path / "watchdog.db"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE gainers_snapshots (
            coin_id TEXT,
            snapshot_at TEXT
        )""")
    conn.execute("""CREATE TABLE trade_decision_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            source_module TEXT NOT NULL,
            signal_combo TEXT,
            paper_trade_id INTEGER,
            event_data TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    conn.execute(
        "INSERT INTO gainers_snapshots (coin_id, snapshot_at) VALUES (?, ?)",
        ("toes", datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    conn.close()

    result = subprocess.run(
        [
            sys.executable,
            str(Path("scripts/check_trade_decision_events.py")),
            "--db",
            str(db_path),
            "--lookback-minutes",
            "15",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert json.loads(result.stdout)["status"] == "missing_recent_decisions"


def test_trade_decision_event_checker_uses_datetime_semantics(tmp_path):
    db_path = tmp_path / "watchdog.db"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE gainers_snapshots (coin_id TEXT, snapshot_at TEXT)")
    conn.execute("""CREATE TABLE trade_decision_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            source_module TEXT NOT NULL,
            signal_combo TEXT,
            paper_trade_id INTEGER,
            event_data TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    fresh_offset_ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn.execute(
        "INSERT INTO gainers_snapshots (coin_id, snapshot_at) VALUES (?, ?)",
        ("toes", fresh_offset_ts),
    )
    conn.execute(
        """INSERT INTO trade_decision_events
           (token_id, signal_type, decision, reason, source_module, event_data, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            "toes",
            "gainers_early",
            "blocked",
            "signal_disabled",
            "test",
            "{}",
            fresh_offset_ts,
        ),
    )
    conn.commit()
    conn.close()

    result = subprocess.run(
        [
            sys.executable,
            str(Path("scripts/check_trade_decision_events.py")),
            "--db",
            str(db_path),
            "--lookback-minutes",
            "15",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "ok"


def test_trade_decision_event_checker_ok_when_idle(tmp_path):
    db_path = tmp_path / "watchdog.db"
    import sqlite3

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE gainers_snapshots (coin_id TEXT, snapshot_at TEXT)")
    conn.execute("""CREATE TABLE trade_decision_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token_id TEXT NOT NULL,
            signal_type TEXT NOT NULL,
            decision TEXT NOT NULL,
            reason TEXT NOT NULL,
            source_module TEXT NOT NULL,
            signal_combo TEXT,
            paper_trade_id INTEGER,
            event_data TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
    conn.commit()
    conn.close()

    result = subprocess.run(
        [
            sys.executable,
            str(Path("scripts/check_trade_decision_events.py")),
            "--db",
            str(db_path),
            "--lookback-minutes",
            "15",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout)["status"] == "idle_no_recent_tracker_rows"
