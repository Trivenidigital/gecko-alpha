"""DASH-05 moved-already / too-late postmortem recorder (forward-recording only).

Covers: migration + idempotence, flag-off inertness, detection dedup, evidence
capture on a seeded tmp DB, and dropping_gate extraction. The recorder mirrors
the dashboard ``_trade_window_state`` "late" predicate (dashboard/db.py):
an OPEN paper trade whose pct-from-entry exceeds the threshold. gainers_snapshots
is 7-day-retention so the capture is forward-only; there is no backfill path.

Windows note: this suite is CI/Linux-only — importing aiohttp transitively on
Windows hits OPENSSL_Uplink (see global CLAUDE.md Platform Constraints). The DB
substrate + recorder logic exercised here are platform-independent.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from scout.config import Settings
from scout.db import Database
from scout.postmortem.moved_already import record_moved_already_postmortems


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


def _settings(tmp_path, *, enabled: bool) -> Settings:
    return Settings(
        TELEGRAM_BOT_TOKEN="test",
        TELEGRAM_CHAT_ID="test",
        ANTHROPIC_API_KEY="test",
        DB_PATH=tmp_path / "test.db",
        MOVED_ALREADY_POSTMORTEM_ENABLED=enabled,
    )


async def _seed_open_trade(
    db,
    token_id: str,
    *,
    entry_price: float,
    opened_at: str,
    symbol: str = "TKN",
    name: str = "Token",
    chain: str = "coingecko",
    signal_type: str = "gainers_early",
) -> None:
    await db._conn.execute(
        """INSERT INTO paper_trades
               (token_id, symbol, name, chain, signal_type, signal_data,
                entry_price, amount_usd, quantity, tp_price, sl_price,
                status, opened_at)
           VALUES (?, ?, ?, ?, ?, '{}', ?, 300.0, 1.0, ?, ?, 'open', ?)""",
        (
            token_id,
            symbol,
            name,
            chain,
            signal_type,
            entry_price,
            entry_price * 1.2,
            entry_price * 0.9,
            opened_at,
        ),
    )
    await db._conn.commit()


async def _seed_price(
    db,
    coin_id: str,
    *,
    current_price: float,
    price_change_24h: float = 0.0,
    market_cap: float = 10_000_000.0,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        """INSERT OR REPLACE INTO price_cache
               (coin_id, current_price, price_change_24h, price_change_7d,
                market_cap, updated_at)
           VALUES (?, ?, ?, 0, ?, ?)""",
        (coin_id, current_price, price_change_24h, market_cap, now),
    )
    await db._conn.commit()


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------


async def test_migration_creates_table(db):
    cur = await db._conn.execute("PRAGMA table_info(moved_already_postmortems)")
    cols = {row[1] for row in await cur.fetchall()}
    assert {
        "id",
        "token_id",
        "detected_at",
        "run_pct",
        "evidence",
        "dropping_gate",
    } <= cols

    cur = await db._conn.execute(
        "SELECT description FROM schema_version WHERE version = ?", (20260713,)
    )
    row = await cur.fetchone()
    assert row is not None
    assert row[0] == "moved_already_postmortems_v1"


async def test_migration_idempotent(tmp_path):
    d = Database(tmp_path / "idem.db")
    await d.initialize()
    # Re-running initialize (deploy restart) must not raise and must not
    # duplicate the schema_version row.
    await d.initialize()
    cur = await d._conn.execute(
        "SELECT COUNT(*) FROM schema_version WHERE version = ?", (20260713,)
    )
    assert (await cur.fetchone())[0] == 1
    await d.close()


# --------------------------------------------------------------------------
# Flag-off inertness
# --------------------------------------------------------------------------


async def test_flag_off_is_inert(db, tmp_path):
    await _seed_open_trade(
        db, "ansem", entry_price=1.0, opened_at="2026-07-10T00:00:00+00:00"
    )
    await _seed_price(db, "ansem", current_price=34.54)  # +3354%

    result = await record_moved_already_postmortems(
        db, _settings(tmp_path, enabled=False)
    )
    assert result["enabled"] is False

    cur = await db._conn.execute("SELECT COUNT(*) FROM moved_already_postmortems")
    assert (await cur.fetchone())[0] == 0


# --------------------------------------------------------------------------
# Detection + dedup
# --------------------------------------------------------------------------


async def test_detects_moved_already_token(db, tmp_path):
    await _seed_open_trade(
        db, "ansem", entry_price=1.0, opened_at="2026-07-10T00:00:00+00:00"
    )
    await _seed_price(db, "ansem", current_price=34.54, price_change_24h=210.0)

    now = datetime(2026, 7, 11, 0, 0, tzinfo=timezone.utc)
    result = await record_moved_already_postmortems(
        db, _settings(tmp_path, enabled=True), now=now
    )
    assert result["enabled"] is True
    assert result["recorded"] == 1

    cur = await db._conn.execute(
        "SELECT token_id, detected_at, run_pct FROM moved_already_postmortems"
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "ansem"
    assert rows[0][1] == now.isoformat()
    assert rows[0][2] == pytest.approx(3354.0, abs=1.0)


async def test_below_threshold_not_recorded(db, tmp_path):
    await _seed_open_trade(
        db, "mild", entry_price=1.0, opened_at="2026-07-10T00:00:00+00:00"
    )
    await _seed_price(db, "mild", current_price=1.10)  # +10%, under 25

    result = await record_moved_already_postmortems(
        db, _settings(tmp_path, enabled=True)
    )
    assert result["recorded"] == 0
    cur = await db._conn.execute("SELECT COUNT(*) FROM moved_already_postmortems")
    assert (await cur.fetchone())[0] == 0


async def test_dedup_per_token(db, tmp_path):
    # Two open trades on the same token, both moved-already.
    await _seed_open_trade(
        db, "ansem", entry_price=1.0, opened_at="2026-07-10T00:00:00+00:00"
    )
    await _seed_open_trade(
        db, "ansem", entry_price=2.0, opened_at="2026-07-10T06:00:00+00:00"
    )
    await _seed_price(db, "ansem", current_price=34.54)

    s = _settings(tmp_path, enabled=True)
    first = await record_moved_already_postmortems(db, s)
    assert first["recorded"] == 1
    # Second run re-detects the same token but must NOT write a second row.
    second = await record_moved_already_postmortems(db, s)
    assert second["recorded"] == 0

    cur = await db._conn.execute("SELECT COUNT(*) FROM moved_already_postmortems")
    assert (await cur.fetchone())[0] == 1


# --------------------------------------------------------------------------
# Evidence capture
# --------------------------------------------------------------------------


async def test_evidence_capture(db, tmp_path):
    token = "ansem"
    await _seed_open_trade(
        db, token, entry_price=1.0, opened_at="2026-07-10T00:00:00+00:00"
    )
    await _seed_price(db, token, current_price=34.54, price_change_24h=210.0)

    # gainers_snapshots (7d window) — two rows, one inside, one outside window.
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
               (coin_id, symbol, name, price_change_24h, market_cap,
                volume_24h, price_at_snapshot, snapshot_at, created_at)
           VALUES (?, 'ANSEM', 'Ansem', 45.0, 5000000, 900000, 1.5,
                   '2026-07-09T00:00:00+00:00', '2026-07-09T00:00:00+00:00')""",
        (token,),
    )
    await db._conn.execute(
        """INSERT INTO gainers_snapshots
               (coin_id, symbol, name, price_change_24h, market_cap,
                volume_24h, price_at_snapshot, snapshot_at, created_at)
           VALUES (?, 'ANSEM', 'Ansem', 12.0, 4000000, 500000, 1.1,
                   '2026-06-01T00:00:00+00:00', '2026-06-01T00:00:00+00:00')""",
        (token,),
    )

    # candidates (contract_address holds CG slug for CG-sourced rows)
    await db._conn.execute(
        """INSERT INTO candidates
               (contract_address, chain, token_name, ticker, first_seen_at,
                quant_score, narrative_score, conviction_score, signals_fired)
           VALUES (?, 'coingecko', 'Ansem', 'ANSEM', '2026-07-08T00:00:00+00:00',
                   40, 30, 55.0, 'gainers_early,volume_spike')""",
        (token,),
    )

    # trade_decision_events: blocked reasons pre-detection
    for reason, n in [("late_pump", 3), ("below_min_mcap", 1)]:
        for _ in range(n):
            await db._conn.execute(
                """INSERT INTO trade_decision_events
                       (token_id, signal_type, decision, reason, source_module,
                        event_data, created_at)
                   VALUES (?, 'gainers_early', 'blocked', ?, 'signals',
                           '{}', '2026-07-09T12:00:00+00:00')""",
                (token, reason),
            )

    # entry_mcap_snapshots
    await db._conn.execute(
        """INSERT INTO entry_mcap_snapshots
               (contract_address, chain, first_seen_at, mcap_usd_at_entry,
                liquidity_usd_at_entry, token_age_days_at_entry, captured_at)
           VALUES (?, 'coingecko', '2026-07-08T00:00:00+00:00', 3500000,
                   120000, 4.0, '2026-07-08T00:00:00+00:00')""",
        (token,),
    )

    # score_history
    await db._conn.execute(
        """INSERT INTO score_history (contract_address, score, scanned_at)
           VALUES (?, 52.0, '2026-07-08T01:00:00+00:00')""",
        (token,),
    )
    await db._conn.commit()

    now = datetime(2026, 7, 11, 0, 0, tzinfo=timezone.utc)
    result = await record_moved_already_postmortems(
        db, _settings(tmp_path, enabled=True), now=now
    )
    assert result["recorded"] == 1

    cur = await db._conn.execute(
        "SELECT evidence, dropping_gate FROM moved_already_postmortems "
        "WHERE token_id = ?",
        (token,),
    )
    ev_json, dropping_gate = await cur.fetchone()
    ev = json.loads(ev_json)

    # run stats
    assert ev["run_stats"]["pct_from_entry"] == pytest.approx(3354.0, abs=1.0)
    assert ev["run_stats"]["price_change_24h"] == pytest.approx(210.0)

    # gainers_snapshots — only the in-window row captured
    assert len(ev["gainers_snapshots"]) == 1
    assert ev["gainers_snapshots"][0]["snapshot_at"] == "2026-07-09T00:00:00+00:00"

    # candidates first_seen / scores
    assert ev["candidate"]["first_seen_at"] == "2026-07-08T00:00:00+00:00"
    assert ev["candidate"]["quant_score"] == 40

    # trade_decision blocks + dropping_gate = most frequent blocked reason
    reasons = {b["reason"]: b["count"] for b in ev["trade_decision_blocks"]}
    assert reasons == {"late_pump": 3, "below_min_mcap": 1}
    assert dropping_gate == "late_pump"

    # entry_mcap / score_history present
    assert ev["entry_mcap_snapshot"]["mcap_usd_at_entry"] == 3500000
    assert ev["score_history"][0]["score"] == 52.0


async def test_dropping_gate_null_when_no_blocks(db, tmp_path):
    token = "clean"
    await _seed_open_trade(
        db, token, entry_price=1.0, opened_at="2026-07-10T00:00:00+00:00"
    )
    await _seed_price(db, token, current_price=2.0)  # +100%

    result = await record_moved_already_postmortems(
        db, _settings(tmp_path, enabled=True)
    )
    assert result["recorded"] == 1
    cur = await db._conn.execute(
        "SELECT dropping_gate FROM moved_already_postmortems WHERE token_id = ?",
        (token,),
    )
    assert (await cur.fetchone())[0] is None


async def test_dropping_gate_ignores_post_detection_blocks(db, tmp_path):
    token = "ansem"
    await _seed_open_trade(
        db, token, entry_price=1.0, opened_at="2026-07-10T00:00:00+00:00"
    )
    await _seed_price(db, token, current_price=2.0)

    # A block AFTER the detection moment must not count toward dropping_gate.
    await db._conn.execute(
        """INSERT INTO trade_decision_events
               (token_id, signal_type, decision, reason, source_module,
                event_data, created_at)
           VALUES (?, 'gainers_early', 'blocked', 'future_reason', 'signals',
                   '{}', '2026-07-12T00:00:00+00:00')""",
        (token,),
    )
    await db._conn.commit()

    now = datetime(2026, 7, 11, 0, 0, tzinfo=timezone.utc)
    result = await record_moved_already_postmortems(
        db, _settings(tmp_path, enabled=True), now=now
    )
    assert result["recorded"] == 1
    cur = await db._conn.execute(
        "SELECT dropping_gate FROM moved_already_postmortems WHERE token_id = ?",
        (token,),
    )
    assert (await cur.fetchone())[0] is None
