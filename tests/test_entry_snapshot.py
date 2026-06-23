"""
BL-NEW-ACTIONABILITY-ENTRY-SNAPSHOT-FOUNDATION — durable point-in-time entry
fact stamping. See tasks/design_actionability_entry_snapshot_foundation_2026_05_20.md
in PR #199 (docs/actionability-entry-snapshot-design-2026-05-20) for the
pre-registered acceptance criteria. Test numbers map 1:1 to that doc's §Tests.
"""

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from structlog.testing import capture_logs

from scout.config import Settings
from scout.db import Database
from scout.trading.paper import PaperTrader


def _settings(**overrides):
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="x",
        TELEGRAM_CHAT_ID="x",
        ANTHROPIC_API_KEY="x",
        **overrides,
    )


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "entry_snapshot.db")
    await d.initialize()
    yield d
    await d.close()


# -- Test 13 ---------------------------------------------------------------
# Migration helper idempotency + sentinel + schema_version (Vector A I1/I2)


async def test_13_migration_creates_table_and_sentinel(db):
    """First-run migration creates the table, the paper_migrations sentinel,
    and a schema_version row. Idempotent: a second invocation is a no-op."""
    cur = await db._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name='paper_trade_entry_snapshots'"
    )
    assert (
        await cur.fetchone() is not None
    ), "paper_trade_entry_snapshots table missing after Database.initialize()"

    cur = await db._conn.execute(
        "SELECT cutover_ts FROM paper_migrations "
        "WHERE name='bl_actionability_entry_snapshot_v1'"
    )
    row = await cur.fetchone()
    assert row is not None, "paper_migrations sentinel not written"

    cur = await db._conn.execute(
        "SELECT description FROM schema_version "
        "WHERE description='bl_actionability_entry_snapshot_v1'"
    )
    row = await cur.fetchone()
    assert row is not None, "schema_version row not written"


async def test_13b_migration_idempotent(db):
    """Calling the migration helper a second time does NOT duplicate the
    sentinel or schema_version row, and does NOT raise."""
    await db._migrate_actionability_entry_snapshot_v1()
    await db._migrate_actionability_entry_snapshot_v1()

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations "
        "WHERE name='bl_actionability_entry_snapshot_v1'"
    )
    assert (await cur.fetchone())[0] == 1

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM schema_version "
        "WHERE description='bl_actionability_entry_snapshot_v1'"
    )
    assert (await cur.fetchone())[0] == 1


# -- Test 14 ---------------------------------------------------------------
# CHECK constraint on entry_snapshot_complete (Vector A I3)


async def test_14_check_constraint_on_complete(db):
    """Direct INSERT with entry_snapshot_complete=2 must raise sqlite
    IntegrityError. Guards future writer bugs that compute the flag wrong."""
    import sqlite3

    await db._conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, "
        "signal_type, signal_data, entry_price, amount_usd, quantity, "
        "tp_pct, sl_pct, tp_price, sl_price, status, opened_at, "
        "signal_combo, remaining_qty) "
        "VALUES (?, ?, ?, ?, ?, '{}', 1, 100, 100, 20, 10, 1.2, 0.9, "
        "'open', '2026-05-20T00:00:00+00:00', ?, 100)",
        (
            "tok",
            "TOK",
            "Token",
            "coingecko",
            "narrative_prediction",
            "narrative_prediction",
        ),
    )
    cur = await db._conn.execute("SELECT id FROM paper_trades WHERE token_id='tok'")
    trade_id = (await cur.fetchone())[0]
    await db._conn.commit()

    with pytest.raises((sqlite3.IntegrityError, Exception)) as excinfo:
        await db._conn.execute(
            "INSERT INTO paper_trade_entry_snapshots "
            "(paper_trade_id, entry_snapshot_version, entry_snapshot_complete, "
            "entry_snapshot_missing_fields, captured_at) "
            "VALUES (?, 'v1', 2, '[]', ?)",
            (trade_id, "2026-05-20T00:00:01+00:00"),
        )
        await db._conn.commit()
    assert (
        "CHECK constraint" in str(excinfo.value)
        or "constraint" in str(excinfo.value).lower()
    )


async def test_14b_migration_rejects_existing_table_missing_required_column(tmp_path):
    """An operator-mutated table shape must fail during initialize(), not at
    the first later INSERT."""
    path = tmp_path / "entry_snapshot_schema_drift.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE paper_trade_entry_snapshots ("
        "paper_trade_id INTEGER PRIMARY KEY, "
        "entry_snapshot_version TEXT NOT NULL, "
        "entry_snapshot_complete INTEGER NOT NULL, "
        "entry_snapshot_missing_fields TEXT NOT NULL, "
        "captured_at TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    d = Database(path)
    with capture_logs() as logs:
        with pytest.raises(RuntimeError, match="missing columns"):
            await d.initialize()
    await d.close()

    assert any(
        entry.get("event") == "SCHEMA_DRIFT_DETECTED"
        and entry.get("migration") == "bl_actionability_entry_snapshot_v1"
        for entry in logs
    )


async def test_14c_migration_rejects_missing_complete_check_constraint(tmp_path):
    """The schema assert must verify the boolean CHECK, not just column names."""
    path = tmp_path / "entry_snapshot_check_drift.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE paper_trade_entry_snapshots ("
        "paper_trade_id INTEGER PRIMARY KEY, "
        "entry_snapshot_version TEXT NOT NULL, "
        "entry_snapshot_complete INTEGER NOT NULL, "
        "entry_snapshot_missing_fields TEXT NOT NULL, "
        "captured_at TEXT NOT NULL, "
        "signal_type TEXT, "
        "mcap_usd_at_entry REAL, "
        "mcap_bucket_at_entry TEXT, "
        "liquidity_usd_at_entry REAL, "
        "token_age_days_at_entry REAL, "
        "first_seen_at_at_entry TEXT, "
        "detected_by_combo_at_entry TEXT, "
        "source_confluence_count_at_entry INTEGER, "
        "tg_channel_at_entry TEXT, "
        "actionability_version_at_entry TEXT, "
        "actionability_reason_at_entry TEXT, "
        "actionable_at_entry INTEGER, "
        "tp_pct_at_entry REAL, "
        "sl_pct_at_entry REAL, "
        "trail_pct_at_entry REAL, "
        "trail_pct_low_peak_at_entry REAL)"
    )
    conn.commit()
    conn.close()

    d = Database(path)
    with pytest.raises(RuntimeError, match="entry_snapshot_complete CHECK missing"):
        await d.initialize()
    await d.close()


# -- Tests 12 + 2 ----------------------------------------------------------
# Version literal pinned + fully-complete snapshot


async def test_12_version_literal_is_v1():
    """ENTRY_SNAPSHOT_VERSION must equal 'v1' exactly. Any change requires
    a new sentinel and migration."""
    from scout.trading.entry_snapshot import ENTRY_SNAPSHOT_VERSION

    assert ENTRY_SNAPSHOT_VERSION == "v1"


async def _open_with_full_signal(trader, db, **overrides):
    """Open a paper trade with a fully-populated signal_data so the
    snapshot should be entry_snapshot_complete=1."""
    base = dict(
        token_id="full-tok",
        symbol="FULL",
        name="Full Token",
        chain="coingecko",
        signal_type="narrative_prediction",
        signal_data={
            "mcap": 20_000_000,
            "liquidity_usd": 250_000,
        },
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="narrative_prediction+gainers_early",
        settings=_settings(),
    )
    base.update(overrides)
    return await trader.execute_buy(db=db, **base)


async def _ensure_candidate_row(db, *, contract_address, chain, first_seen_at):
    """Manually seed a candidates row for the first_seen_at lookup."""
    await db._conn.execute(
        "INSERT OR IGNORE INTO candidates "
        "(contract_address, chain, token_name, ticker, first_seen_at) "
        "VALUES (?, ?, 'FullToken', 'FULL', ?)",
        (contract_address, chain, first_seen_at),
    )
    await db._conn.commit()


async def test_2_fully_complete_snapshot(db):
    """All optional fields resolvable → complete=1 + missing_fields='[]'."""
    await _ensure_candidate_row(
        db,
        contract_address="full-tok",
        chain="coingecko",
        first_seen_at="2026-05-19T12:00:00+00:00",
    )
    trade_id = await _open_with_full_signal(PaperTrader(), db)
    assert trade_id is not None

    cur = await db._conn.execute(
        "SELECT entry_snapshot_version, entry_snapshot_complete, "
        "entry_snapshot_missing_fields, signal_type, "
        "mcap_usd_at_entry, mcap_bucket_at_entry, liquidity_usd_at_entry, "
        "first_seen_at_at_entry, detected_by_combo_at_entry, "
        "source_confluence_count_at_entry, "
        "actionability_version_at_entry, actionable_at_entry, "
        "tp_pct_at_entry, sl_pct_at_entry "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    row = await cur.fetchone()
    assert row is not None, "no snapshot row written"
    (
        version,
        complete,
        missing_fields,
        signal_type,
        mcap,
        mcap_bucket,
        liq,
        first_seen,
        combo,
        conf_count,
        ab_version,
        actionable_at_entry,
        tp_at_entry,
        sl_at_entry,
    ) = row
    assert version == "v1"
    assert complete == 1
    assert missing_fields == "[]"
    assert signal_type == "narrative_prediction"
    assert mcap == 20_000_000
    assert mcap_bucket == "10_50m"
    assert liq == 250_000
    assert first_seen == "2026-05-19T12:00:00+00:00"
    assert combo == "narrative_prediction+gainers_early"
    assert conf_count == 2  # narrative_prediction + gainers_early
    assert ab_version == "v1"
    assert actionable_at_entry == 1
    assert tp_at_entry == 20.0
    assert sl_at_entry == 10.0


async def test_2b_first_seen_lookup_handles_mixed_case_candidate_contract(db):
    """Prod EVM candidates can carry checksummed mixed-case addresses."""
    mixed_case_contract = "0xAbCdEf1234567890"
    await _ensure_candidate_row(
        db,
        contract_address=mixed_case_contract,
        chain="ethereum",
        first_seen_at="2026-05-19T12:00:00+00:00",
    )
    trade_id = await _open_with_full_signal(
        PaperTrader(),
        db,
        token_id=mixed_case_contract.lower(),
        chain="ethereum",
        signal_data={"mcap": 20_000_000, "liquidity_usd": 250_000},
    )
    assert trade_id is not None

    cur = await db._conn.execute(
        "SELECT entry_snapshot_complete, first_seen_at_at_entry "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    complete, first_seen = await cur.fetchone()
    assert complete == 1
    assert first_seen == "2026-05-19T12:00:00+00:00"


# -- Test 3 ----------------------------------------------------------------
# Partial snapshot — missing liquidity


async def test_3_partial_missing_liquidity(db):
    await _ensure_candidate_row(
        db,
        contract_address="part-tok",
        chain="coingecko",
        first_seen_at="2026-05-19T12:00:00+00:00",
    )
    trade_id = await _open_with_full_signal(
        PaperTrader(),
        db,
        token_id="part-tok",
        signal_data={"mcap": 20_000_000},  # no liquidity_usd
    )
    cur = await db._conn.execute(
        "SELECT entry_snapshot_complete, entry_snapshot_missing_fields, "
        "liquidity_usd_at_entry, mcap_usd_at_entry "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    complete, missing_json, liq, mcap = await cur.fetchone()
    assert complete == 0
    missing = json.loads(missing_json)
    assert "liquidity_usd_at_entry" in missing
    assert liq is None
    assert mcap == 20_000_000


# -- Test 4 ----------------------------------------------------------------
# Partial snapshot — missing first_seen (no candidates row for this token)


async def test_4_partial_missing_first_seen(db):
    trade_id = await _open_with_full_signal(
        PaperTrader(),
        db,
        token_id="no-candidate-tok",
        signal_data={"mcap": 20_000_000, "liquidity_usd": 250_000},
    )
    cur = await db._conn.execute(
        "SELECT entry_snapshot_complete, entry_snapshot_missing_fields, "
        "first_seen_at_at_entry, token_age_days_at_entry "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    complete, missing_json, first_seen, age = await cur.fetchone()
    assert complete == 0
    missing = json.loads(missing_json)
    assert "first_seen_at_at_entry" in missing
    assert "token_age_days_at_entry" in missing
    assert first_seen is None
    assert age is None


# -- Test 5 + 5b -----------------------------------------------------------
# tg_social channel population (5) + temporal-bound C1 fix (5b)


async def _seed_tg_signal(db, *, token_id, channel, created_at, msg_id=1):
    """Seed the tg_social_messages parent + tg_social_signals child rows."""
    cur = await db._conn.execute(
        "INSERT INTO tg_social_messages "
        "(channel_handle, msg_id, posted_at, parsed_at) "
        "VALUES (?, ?, ?, ?)",
        (channel, msg_id, created_at, created_at),
    )
    message_pk = cur.lastrowid
    await db._conn.execute(
        "INSERT INTO tg_social_signals "
        "(message_pk, token_id, symbol, resolution_state, "
        "source_channel_handle, created_at) "
        "VALUES (?, ?, 'TG', 'resolved', ?, ?)",
        (message_pk, token_id, channel, created_at),
    )
    await db._conn.commit()


async def test_5_tg_social_channel_at_entry(db):
    await _seed_tg_signal(
        db,
        token_id="tg-tok",
        channel="@operator_calls",
        created_at="2026-05-20T00:00:00+00:00",
        msg_id=1001,
    )

    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id="tg-tok",
        symbol="TG",
        name="TG Token",
        chain="coingecko",
        signal_type="tg_social",
        signal_data={"mcap": 20_000_000, "liquidity_usd": 250_000},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="tg_social",
        settings=_settings(),
    )
    cur = await db._conn.execute(
        "SELECT tg_channel_at_entry FROM paper_trade_entry_snapshots "
        "WHERE paper_trade_id=?",
        (trade_id,),
    )
    (chan,) = await cur.fetchone()
    assert chan == "@operator_calls"


async def test_5b_tg_social_no_post_open_leakage(db):
    """Vector B C1: a tg_social_signals row created AFTER opened_at must
    NOT be picked as the at-entry channel."""
    await _seed_tg_signal(
        db,
        token_id="tg-tok2",
        channel="@pre_open",
        created_at="2026-05-20T00:00:00+00:00",
        msg_id=2001,
    )
    await _seed_tg_signal(
        db,
        token_id="tg-tok2",
        channel="@post_open",
        created_at="2099-12-31T23:59:59+00:00",
        msg_id=2002,
    )

    # Open between the two rows. The trade_open `now` will be ~real-time UTC
    # which is between 2026-05-20T00:00:00 and 2099. So pre_open passes the
    # `created_at <= opened_at` bound; post_open does not.
    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id="tg-tok2",
        symbol="TG2",
        name="TG2 Token",
        chain="coingecko",
        signal_type="tg_social",
        signal_data={"mcap": 20_000_000, "liquidity_usd": 250_000},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="tg_social",
        settings=_settings(),
    )
    cur = await db._conn.execute(
        "SELECT tg_channel_at_entry FROM paper_trade_entry_snapshots "
        "WHERE paper_trade_id=?",
        (trade_id,),
    )
    (chan,) = await cur.fetchone()
    assert (
        chan == "@pre_open"
    ), f"post-open tg_social_signals row leaked into snapshot: got {chan!r}"


# -- Test 6 ----------------------------------------------------------------
# Actionability fields copied correctly


async def test_6_actionability_fields_copied(db):
    """For a narrative_prediction trade at mcap 10-50M, actionability classifier
    returns actionable=1, reason='v1_pass_core_signal_mcap_10_50m', version='v1'.
    Snapshot row's three *_at_entry fields must match exactly."""
    trade_id = await _open_with_full_signal(PaperTrader(), db, token_id="actn-tok")
    cur = await db._conn.execute(
        "SELECT actionability_version_at_entry, actionability_reason_at_entry, "
        "actionable_at_entry FROM paper_trade_entry_snapshots "
        "WHERE paper_trade_id=?",
        (trade_id,),
    )
    version, reason, actionable = await cur.fetchone()
    assert version == "v1"
    assert reason == "v1_pass_core_signal_mcap_10_50m"
    assert actionable == 1


# -- Test 7 + 7b -----------------------------------------------------------
# Exit params (7) + trail_pct audit-replay with temporal bound (7b)


async def test_7_exit_params_copied_fallback(db):
    """No audit history → trail_pct_at_entry falls back to signal_params seed."""
    cur = await db._conn.execute(
        "SELECT trail_pct FROM signal_params WHERE signal_type='narrative_prediction'"
    )
    seed_trail = (await cur.fetchone())[0]

    trade_id = await _open_with_full_signal(PaperTrader(), db, token_id="exit-tok")
    cur = await db._conn.execute(
        "SELECT tp_pct_at_entry, sl_pct_at_entry, trail_pct_at_entry "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    tp, sl, trail = await cur.fetchone()
    assert tp == 20.0
    assert sl == 10.0
    assert trail == seed_trail


async def test_7b_trail_pct_audit_replay_temporal_bound(db):
    """Vector B C2: a signal_params_audit row with applied_at > opened_at must
    NOT contaminate the at-entry value. Audit history with applied_at <=
    opened_at IS read."""
    # T - 1h audit row: trail_pct change from 30 -> 40
    pre_open_iso = "2026-05-19T23:00:00+00:00"
    # T + 1h audit row: trail_pct change from 40 -> 50 (this should NOT
    # contaminate the snapshot)
    post_open_iso = "2099-01-01T00:00:00+00:00"

    await db._conn.execute(
        "INSERT INTO signal_params_audit "
        "(signal_type, field_name, old_value, new_value, reason, "
        "applied_by, applied_at) VALUES "
        "('narrative_prediction', 'trail_pct', '30.0', '40.0', "
        "'pre-open seed', 'test', ?)",
        (pre_open_iso,),
    )
    await db._conn.execute(
        "INSERT INTO signal_params_audit "
        "(signal_type, field_name, old_value, new_value, reason, "
        "applied_by, applied_at) VALUES "
        "('narrative_prediction', 'trail_pct', '40.0', '50.0', "
        "'post-open contamination', 'test', ?)",
        (post_open_iso,),
    )
    await db._conn.commit()

    trade_id = await _open_with_full_signal(PaperTrader(), db, token_id="audit-tok")
    cur = await db._conn.execute(
        "SELECT trail_pct_at_entry FROM paper_trade_entry_snapshots "
        "WHERE paper_trade_id=?",
        (trade_id,),
    )
    (trail,) = await cur.fetchone()
    # Trade opens "now" (real-time UTC, which is between 2026-05-19T23:00:00
    # and 2099-01-01T00:00:00). The audit-replay must pick the PRE-open row
    # (40.0), NOT the POST-open row (50.0), NOT the current signal_params
    # seed value.
    assert trail == 40.0, (
        f"audit-replay temporal bound failed: trail={trail!r} "
        f"(expected 40.0 from pre-open audit, NOT 50.0 post-open, "
        f"NOT current signal_params seed)"
    )


# -- Test 8 ----------------------------------------------------------------
# Pre-cutover rows distinguishable (no snapshot row exists)


async def test_8_pre_cutover_rows_distinguishable(db):
    # Insert a paper_trade directly with NO sidecar row
    await db._conn.execute(
        "INSERT INTO paper_trades (token_id, symbol, name, chain, "
        "signal_type, signal_data, entry_price, amount_usd, quantity, "
        "tp_pct, sl_pct, tp_price, sl_price, status, opened_at, "
        "signal_combo, remaining_qty) "
        "VALUES ('pre-cut', 'PRE', 'Pre', 'coingecko', "
        "'narrative_prediction', '{}', 1, 100, 100, 20, 10, 1.2, 0.9, "
        "'open', '2026-05-19T11:00:00+00:00', 'narrative_prediction', 100)"
    )
    await db._conn.commit()
    cur = await db._conn.execute(
        "SELECT pt.id, s.entry_snapshot_version "
        "FROM paper_trades pt "
        "LEFT JOIN paper_trade_entry_snapshots s ON s.paper_trade_id = pt.id "
        "WHERE pt.token_id='pre-cut'"
    )
    trade_id, version = await cur.fetchone()
    assert trade_id is not None
    assert version is None  # pre-cutover sidecar absent


# -- Test 9 ----------------------------------------------------------------
# No classifier or trading-decision change — paper_trades column set unchanged


async def test_9_no_paper_trades_behavior_change(db):
    """A fixed-input trade-open must produce paper_trades row with the same
    columns it would have before the sidecar landed. Verify by listing
    paper_trades column names and asserting none of the sidecar fields
    have leaked into paper_trades."""
    cur = await db._conn.execute("PRAGMA table_info(paper_trades)")
    cols = {row[1] for row in await cur.fetchall()}
    # None of the *_at_entry fields should appear on paper_trades.
    sidecar_only = {
        "entry_snapshot_version",
        "entry_snapshot_complete",
        "entry_snapshot_missing_fields",
        "captured_at",
        "mcap_usd_at_entry",
        "mcap_bucket_at_entry",
        "liquidity_usd_at_entry",
        "token_age_days_at_entry",
        "first_seen_at_at_entry",
        "detected_by_combo_at_entry",
        "source_confluence_count_at_entry",
        "tg_channel_at_entry",
        "actionability_version_at_entry",
        "actionability_reason_at_entry",
        "actionable_at_entry",
        "tp_pct_at_entry",
        "sl_pct_at_entry",
        "trail_pct_at_entry",
        "trail_pct_low_peak_at_entry",
    }
    assert cols.isdisjoint(sidecar_only)


# -- Test 10 ---------------------------------------------------------------
# Snapshot-write failure does NOT fail trade-open


async def test_10_stamp_failure_does_not_block_trade(db, monkeypatch):
    """Monkey-patch stamp_entry_snapshot to raise. Trade still opens; no
    sidecar row; entry_snapshot_stamp_failed log fires."""
    import scout.trading.entry_snapshot as es_mod

    async def _boom(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(es_mod, "stamp_entry_snapshot", _boom)

    trade_id = await _open_with_full_signal(PaperTrader(), db, token_id="boom-tok")
    assert trade_id is not None  # trade still opened

    cur = await db._conn.execute(
        "SELECT id FROM paper_trades WHERE token_id='boom-tok'"
    )
    assert await cur.fetchone() is not None

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    assert (await cur.fetchone())[0] == 0  # no sidecar row


# -- Test 11 ---------------------------------------------------------------
# Duplicate stamp raises (no INSERT OR IGNORE) — Vector B I1


async def test_11_duplicate_stamp_raises(db):
    """Calling stamp_entry_snapshot twice on the same trade_id raises
    IntegrityError. We intentionally do NOT use INSERT OR IGNORE — silencing
    duplicate-PK would mask incorrect callers."""
    import sqlite3
    from scout.trading.entry_snapshot import stamp_entry_snapshot

    trade_id = await _open_with_full_signal(PaperTrader(), db, token_id="dup-tok")

    # First stamp happened during execute_buy. A second stamp on the same
    # trade_id should raise.
    with pytest.raises((sqlite3.IntegrityError, Exception)) as excinfo:
        await stamp_entry_snapshot(
            db,
            trade_id=trade_id,
            opened_at="2026-05-20T00:00:00+00:00",
            signal_type="narrative_prediction",
            signal_data={"mcap": 20_000_000, "liquidity_usd": 250_000},
            signal_combo="narrative_prediction",
            tp_pct=20.0,
            sl_pct=10.0,
            actionable_value=1,
            actionability_reason="v1_pass_core_signal_mcap_10_50m",
            actionability_version="v1",
            contract_address="dup-tok",
            chain="coingecko",
            settings=_settings(),
        )
    assert (
        "UNIQUE" in str(excinfo.value)
        or "PRIMARY KEY" in str(excinfo.value)
        or "constraint" in str(excinfo.value).lower()
    )


# -- Tests for Vector B I-B1 + I-B2 folds (post-impl-review) ---------------


async def test_ib1_unparseable_audit_does_not_leak_current_signal_params(db):
    """Vector B I-B1 fold: when an audit row exists at applied_at <= opened_at
    but new_value is unparseable, _read_trail_at_entry returns None instead
    of silently falling through to current signal_params (which would leak
    post-open recalibrations)."""
    pre_open_iso = "2026-05-19T23:00:00+00:00"

    # Single pre-open audit row with unparseable new_value (e.g., a buggy
    # writer storing "None" or "40%" instead of "40.0")
    await db._conn.execute(
        "INSERT INTO signal_params_audit "
        "(signal_type, field_name, old_value, new_value, reason, "
        "applied_by, applied_at) VALUES "
        "('narrative_prediction', 'trail_pct', '30.0', '40%', "
        "'buggy unit suffix', 'test', ?)",
        (pre_open_iso,),
    )
    await db._conn.commit()

    # Mutate current signal_params.trail_pct to a value that, if leaked,
    # would be detectable.
    await db._conn.execute(
        "UPDATE signal_params SET trail_pct=99.9 WHERE signal_type='narrative_prediction'"
    )
    await db._conn.commit()

    trade_id = await _open_with_full_signal(PaperTrader(), db, token_id="ib1-tok")
    cur = await db._conn.execute(
        "SELECT trail_pct_at_entry, entry_snapshot_complete, "
        "entry_snapshot_missing_fields "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    trail, complete, missing_json = await cur.fetchone()
    # Unparseable audit row → return None. MUST NOT be 99.9 (current
    # signal_params value, which would mean the post-open leak fired).
    assert trail is None, (
        f"unparseable audit row leaked current signal_params: trail={trail!r} "
        f"(expected None; 99.9 would indicate I-B1 leak)"
    )
    assert complete == 0
    missing = json.loads(missing_json)
    assert "trail_pct_at_entry" in missing


async def test_ib1_no_audit_history_falls_back_to_signal_params(db):
    """Vector B I-B1 control: when NO audit history exists for a field, the
    seed-baseline fallback (read current signal_params) IS used. The
    seed-baseline read is correct because nothing has changed since seed."""
    cur = await db._conn.execute(
        "SELECT trail_pct FROM signal_params WHERE signal_type='narrative_prediction'"
    )
    seed_trail = (await cur.fetchone())[0]
    assert seed_trail is not None  # sanity

    trade_id = await _open_with_full_signal(
        PaperTrader(), db, token_id="ib1-control-tok"
    )
    cur = await db._conn.execute(
        "SELECT trail_pct_at_entry FROM paper_trade_entry_snapshots "
        "WHERE paper_trade_id=?",
        (trade_id,),
    )
    (trail,) = await cur.fetchone()
    assert trail == seed_trail


async def test_ib1_field_name_whitelist_enforced():
    """Vector A Minor #3 fold: _read_trail_at_entry rejects unknown field
    names to close the f-string SQL injection surface for future callers."""
    from scout.trading.entry_snapshot import _read_trail_at_entry

    with pytest.raises(ValueError, match="field_name"):
        await _read_trail_at_entry(
            None,
            signal_type="narrative_prediction",
            field_name="trail_pct; DROP TABLE paper_trades; --",
            opened_at="2026-05-20T00:00:00+00:00",
        )


async def test_postfold_a_tg_social_mcap_at_sighting_captured(db):
    """Post-fold Vector A Important: tg_social dispatcher constructs
    signal_data with `mcap_at_sighting` (NOT `mcap`). The actionability
    classifier's _extract_mcap reads 5 keys (mcap / market_cap /
    market_cap_usd / mcap_at_sighting / alert_market_cap). Pre-fold,
    snapshot's _extract_mcap read only the first 3 — so every real tg_social
    trade had mcap_usd_at_entry=None.

    Fix: snapshot's _extract_mcap reuses actionability._extract_mcap to keep
    the key list in lockstep. This test asserts mcap is captured when the
    signal_data uses the `mcap_at_sighting` key (the tg_social shape)."""
    await _seed_tg_signal(
        db,
        token_id="tg-mcap-tok",
        channel="@kol",
        created_at="2026-05-20T00:00:00+00:00",
        msg_id=3001,
    )

    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id="tg-mcap-tok",
        symbol="TG",
        name="TG Mcap",
        chain="coingecko",
        signal_type="tg_social",
        # Real tg_social dispatcher shape: mcap_at_sighting, not mcap
        signal_data={
            "channel_handle": "@kol",
            "contract_address": "tg-mcap-tok",
            "mcap_at_sighting": 18_000_000,
        },
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="tg_social",
        settings=_settings(),
    )
    assert trade_id is not None

    cur = await db._conn.execute(
        "SELECT mcap_usd_at_entry, mcap_bucket_at_entry "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    mcap, bucket = await cur.fetchone()
    assert mcap == 18_000_000, (
        f"tg_social mcap_at_sighting did not land in snapshot: got {mcap!r} "
        f"(expected 18_000_000 — Vector A post-fold key-list alignment)"
    )
    assert bucket == "10_50m"


async def test_tg_social_dex_token_uses_raw_contract_for_candidate_first_seen(db):
    """DexScreener tg_social trades use token_id=dex:<chain>:<address>.

    candidates.contract_address stores the raw address, so the entry snapshot
    must use signal_data.contract_address for candidate first_seen_at while
    still using token_id for the tg_social channel lookup.
    """
    token_id = "dex:ethereum:0xabcdef"
    raw_contract = "0xabcdef"
    await _ensure_candidate_row(
        db,
        contract_address=raw_contract,
        chain="ethereum",
        first_seen_at="2026-05-19T00:00:00+00:00",
    )
    await _seed_tg_signal(
        db,
        token_id=token_id,
        channel="@dex_calls",
        created_at="2026-05-20T00:00:00+00:00",
        msg_id=3101,
    )

    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id=token_id,
        symbol="DEX",
        name="Dex Token",
        chain="ethereum",
        signal_type="tg_social",
        signal_data={
            "channel_handle": "@dex_calls",
            "contract_address": raw_contract,
            "mcap_at_sighting": 18_000_000,
            "liquidity_usd": 250_000,
        },
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="tg_social",
        settings=_settings(),
    )
    assert trade_id is not None

    cur = await db._conn.execute(
        "SELECT first_seen_at_at_entry, tg_channel_at_entry, "
        "entry_snapshot_complete, entry_snapshot_missing_fields "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    first_seen, channel, complete, missing_json = await cur.fetchone()
    assert first_seen == "2026-05-19T00:00:00+00:00"
    assert channel == "@dex_calls"
    assert complete == 1
    assert "first_seen_at_at_entry" not in missing_json


async def test_postfold_a_alert_market_cap_key_captured(db):
    """Companion to the above: the 5th key actionability supports is
    `alert_market_cap` (used by secondwave-style paths). Snapshot must
    capture this too via the lockstep helper."""
    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id="alert-mcap-tok",
        symbol="ALR",
        name="Alert Mcap",
        chain="coingecko",
        signal_type="narrative_prediction",
        signal_data={"alert_market_cap": 12_000_000, "liquidity_usd": 200_000},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="narrative_prediction",
        settings=_settings(),
    )
    assert trade_id is not None

    cur = await db._conn.execute(
        "SELECT mcap_usd_at_entry, mcap_bucket_at_entry "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    mcap, bucket = await cur.fetchone()
    assert mcap == 12_000_000
    assert bucket == "10_50m"


# -- Liquidity enrichment fallback + provenance ---------------------------
# Root cause: snapshot read signal_data["liquidity_usd"], a key NO producer
# emits for the CG cohort, so liquidity_usd_at_entry was 100% NULL. Fix:
# fall back to candidates.liquidity_usd_enriched (written by the DexScreener
# enrichment cron) and record provenance (source + confidence).


async def _ensure_candidate_row_enriched(
    db,
    *,
    contract_address,
    chain,
    first_seen_at,
    liq_enriched=None,
    enriched_source=None,
    enriched_confidence=None,
):
    """Seed a candidates row, optionally with the enrichment columns set."""
    await db._conn.execute(
        "INSERT OR IGNORE INTO candidates "
        "(contract_address, chain, token_name, ticker, first_seen_at, "
        "liquidity_usd_enriched, liquidity_enriched_source, "
        "liquidity_enriched_confidence) "
        "VALUES (?, ?, 'EnrTok', 'ENR', ?, ?, ?, ?)",
        (
            contract_address,
            chain,
            first_seen_at,
            liq_enriched,
            enriched_source,
            enriched_confidence,
        ),
    )
    await db._conn.commit()


async def test_liq_enrichment_fallback_definite(db):
    """signal_data lacks liquidity_usd; candidates has an enriched value with
    confidence='definite' → snapshot captures the value + provenance, and the
    field is NOT counted missing (complete=1)."""
    await _ensure_candidate_row_enriched(
        db,
        contract_address="enr-tok",
        chain="coingecko",
        first_seen_at="2026-05-19T12:00:00+00:00",
        liq_enriched=120000.0,
        enriched_source="dexscreener:base",
        enriched_confidence="definite",
    )
    trade_id = await _open_with_full_signal(
        PaperTrader(),
        db,
        token_id="enr-tok",
        signal_data={"mcap": 20_000_000},  # NO liquidity_usd
    )
    cur = await db._conn.execute(
        "SELECT liquidity_usd_at_entry, liquidity_source_at_entry, "
        "liquidity_confidence_at_entry, entry_snapshot_complete, "
        "entry_snapshot_missing_fields "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    liq, source, confidence, complete, missing_json = await cur.fetchone()
    assert liq == 120000.0
    assert source == "dexscreener:base"
    assert confidence == "definite"
    assert complete == 1
    assert "liquidity_usd_at_entry" not in missing_json


async def test_liq_signal_data_wins_over_enrichment(db):
    """signal_data.liquidity_usd present → used directly with
    source='signal_data', confidence='definite'; enrichment is ignored."""
    await _ensure_candidate_row_enriched(
        db,
        contract_address="enr-tok2",
        chain="coingecko",
        first_seen_at="2026-05-19T12:00:00+00:00",
        liq_enriched=999.0,
        enriched_source="dexscreener:base",
        enriched_confidence="definite",
    )
    trade_id = await _open_with_full_signal(
        PaperTrader(),
        db,
        token_id="enr-tok2",
        signal_data={"mcap": 20_000_000, "liquidity_usd": 250_000},
    )
    cur = await db._conn.execute(
        "SELECT liquidity_usd_at_entry, liquidity_source_at_entry, "
        "liquidity_confidence_at_entry "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    liq, source, confidence = await cur.fetchone()
    assert liq == 250_000
    assert source == "signal_data"
    assert confidence == "definite"


async def test_liq_enrichment_visited_no_match_not_missing(db):
    """Writer visited but DexScreener returned no pair (value NULL,
    confidence='dex_no_match') → liquidity None, confidence recorded, and the
    field is treated as known-absent, NOT a data gap (complete=1)."""
    await _ensure_candidate_row_enriched(
        db,
        contract_address="enr-tok3",
        chain="coingecko",
        first_seen_at="2026-05-19T12:00:00+00:00",
        liq_enriched=None,
        enriched_source=None,
        enriched_confidence="dex_no_match",
    )
    trade_id = await _open_with_full_signal(
        PaperTrader(),
        db,
        token_id="enr-tok3",
        signal_data={"mcap": 20_000_000},
    )
    cur = await db._conn.execute(
        "SELECT liquidity_usd_at_entry, liquidity_confidence_at_entry, "
        "entry_snapshot_complete, entry_snapshot_missing_fields "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    liq, confidence, complete, missing_json = await cur.fetchone()
    assert liq is None
    assert confidence == "dex_no_match"
    assert "liquidity_usd_at_entry" not in missing_json
    assert complete == 1


async def test_liq_never_enriched_is_missing(db):
    """candidates row exists but writer never visited (confidence NULL) and
    signal_data lacks liquidity → genuine gap: liquidity None, confidence None,
    counted as missing (complete=0)."""
    await _ensure_candidate_row_enriched(
        db,
        contract_address="enr-tok4",
        chain="coingecko",
        first_seen_at="2026-05-19T12:00:00+00:00",
    )  # no enrichment columns set
    trade_id = await _open_with_full_signal(
        PaperTrader(),
        db,
        token_id="enr-tok4",
        signal_data={"mcap": 20_000_000},
    )
    cur = await db._conn.execute(
        "SELECT liquidity_usd_at_entry, liquidity_confidence_at_entry, "
        "entry_snapshot_complete, entry_snapshot_missing_fields "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    liq, confidence, complete, missing_json = await cur.fetchone()
    assert liq is None
    assert confidence is None
    assert "liquidity_usd_at_entry" in missing_json
    assert complete == 0


async def test_provenance_migration_columns_and_sentinel(db):
    """The v2 provenance migration adds the 2 columns and records its
    sentinel + schema_version rows."""
    cur = await db._conn.execute("PRAGMA table_info(paper_trade_entry_snapshots)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "liquidity_source_at_entry" in cols
    assert "liquidity_confidence_at_entry" in cols

    cur = await db._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations "
        "WHERE name='bl_entry_snapshot_liquidity_provenance_v1'"
    )
    assert (await cur.fetchone())[0] == 1


async def test_provenance_migration_upgrades_existing_v1_db(tmp_path):
    """Prod path: a DB that already has the v1 snapshot table but WITHOUT the
    provenance columns gets them added on the next initialize(), idempotently
    and without tripping the strict v1 schema assert (early-return branch)."""
    path = tmp_path / "upgrade.db"
    d = Database(path)
    await d.initialize()
    # Simulate the pre-provenance prod shape: drop the v2 columns + sentinel.
    await d._conn.execute(
        "ALTER TABLE paper_trade_entry_snapshots "
        "DROP COLUMN liquidity_source_at_entry"
    )
    await d._conn.execute(
        "ALTER TABLE paper_trade_entry_snapshots "
        "DROP COLUMN liquidity_confidence_at_entry"
    )
    await d._conn.execute(
        "DELETE FROM paper_migrations "
        "WHERE name='bl_entry_snapshot_liquidity_provenance_v1'"
    )
    await d._conn.commit()
    cur = await d._conn.execute("PRAGMA table_info(paper_trade_entry_snapshots)")
    pre_cols = {row[1] for row in await cur.fetchall()}
    assert "liquidity_source_at_entry" not in pre_cols  # sanity: drop worked
    await d.close()

    # Re-open: the v1 assert must still pass (extra-column-tolerant), and the
    # v2 migration must re-add the provenance columns + sentinel.
    d2 = Database(path)
    await d2.initialize()
    cur = await d2._conn.execute("PRAGMA table_info(paper_trade_entry_snapshots)")
    cols = {row[1] for row in await cur.fetchall()}
    assert "liquidity_source_at_entry" in cols
    assert "liquidity_confidence_at_entry" in cols
    cur = await d2._conn.execute(
        "SELECT COUNT(*) FROM paper_migrations "
        "WHERE name='bl_entry_snapshot_liquidity_provenance_v1'"
    )
    assert (await cur.fetchone())[0] == 1
    await d2.close()


async def test_ib2_enriched_mcap_landed_in_snapshot(db):
    """Vector B I-B2 fold: for a chain_completed trade where signal_data
    lacks mcap but chain_matches has mcap_at_completion, the snapshot must
    capture the SAME enriched mcap the classifier saw, not None."""
    # Seed a chain_patterns parent row + chain_matches row supplying the
    # enrichment value
    cur = await db._conn.execute(
        "INSERT INTO chain_patterns (name, description, steps_json, "
        "min_steps_to_trigger) VALUES ('p2', 'test', '[]', 2)"
    )
    pattern_id = cur.lastrowid
    await db._conn.execute(
        "INSERT INTO chain_matches "
        "(token_id, pipeline, pattern_id, pattern_name, steps_matched, "
        "total_steps, anchor_time, completed_at, chain_duration_hours, "
        "conviction_boost, mcap_at_completion) "
        "VALUES ('chain-tok', 'p1', ?, 'p2', 2, 2, "
        "'2026-05-19T23:00:00+00:00', '2026-05-19T23:30:00+00:00', "
        "0.5, 5, 15000000)",
        (pattern_id,),
    )
    await db._conn.commit()

    # Open a chain_completed trade with EMPTY signal_data (no mcap)
    trade_id = await PaperTrader().execute_buy(
        db=db,
        token_id="chain-tok",
        symbol="CHAIN",
        name="Chain Tok",
        chain="coingecko",
        signal_type="chain_completed",
        signal_data={},
        current_price=1.0,
        amount_usd=300.0,
        tp_pct=20.0,
        sl_pct=10.0,
        signal_combo="chain_completed",
        settings=_settings(),
    )
    assert trade_id is not None

    cur = await db._conn.execute(
        "SELECT mcap_usd_at_entry, mcap_bucket_at_entry "
        "FROM paper_trade_entry_snapshots WHERE paper_trade_id=?",
        (trade_id,),
    )
    mcap, bucket = await cur.fetchone()
    assert mcap == 15_000_000, (
        f"enriched mcap did not land in snapshot: got {mcap!r} "
        f"(expected 15_000_000 from chain_matches enrichment path)"
    )
    assert bucket == "10_50m"
