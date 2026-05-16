"""Tests for scout.db module."""

from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


# ---------------------------------------------------------------------------
# BL-NEW-SCORE-HISTORY-PRUNING + BL-NEW-VOLUME-SNAPSHOTS-PRUNING — index + prune
# ---------------------------------------------------------------------------


async def test_prune_score_history_uses_scanned_at_index(db):
    """V1#5 / V2#5 / V4#3 fold: DELETE WHERE scanned_at <= ? must use the
    new single-column idx_score_history_scanned_at, not table-scan.

    Existing idx_score_hist_addr (contract_address, scanned_at) cannot serve
    the time-only predicate because contract_address is the leading column.
    """
    cur = await db._conn.execute(
        "EXPLAIN QUERY PLAN DELETE FROM score_history WHERE scanned_at <= ?",
        ("2026-01-01T00:00:00+00:00",),
    )
    plan = await cur.fetchall()
    plan_str = " ".join(str(row[3]) for row in plan)
    assert (
        "idx_score_history_scanned_at" in plan_str
    ), f"Index not used: {plan_str}"


async def test_prune_volume_snapshots_uses_scanned_at_index(db):
    """Same as above for volume_snapshots."""
    cur = await db._conn.execute(
        "EXPLAIN QUERY PLAN DELETE FROM volume_snapshots WHERE scanned_at <= ?",
        ("2026-01-01T00:00:00+00:00",),
    )
    plan = await cur.fetchall()
    plan_str = " ".join(str(row[3]) for row in plan)
    assert (
        "idx_volume_snapshots_scanned_at" in plan_str
    ), f"Index not used: {plan_str}"


async def test_prune_score_history_keeps_recent(db):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=5)).isoformat()
    old = (now - timedelta(days=20)).isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xRECENT", 50.0, recent),
    )
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xOLD", 50.0, old),
    )
    await db._conn.commit()

    deleted = await db.prune_score_history(keep_days=14)

    assert deleted == 1
    cur = await db._conn.execute("SELECT contract_address FROM score_history")
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["0xRECENT"]


async def test_prune_score_history_empty_table_returns_zero(db):
    deleted = await db.prune_score_history(keep_days=14)
    assert deleted == 0


async def test_prune_score_history_keep_days_zero_deletes_all(db):
    now = datetime.now(timezone.utc).isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xANY", 50.0, now),
    )
    await db._conn.commit()
    deleted = await db.prune_score_history(keep_days=0)
    assert deleted == 1


async def test_prune_score_history_tie_on_cutoff_deletes(db):
    """V1#11 fold: lock in <= semantic. Row with scanned_at == cutoff must be pruned.

    Matches cryptopanic_posts boundary semantic at db.py:4754-4758 (Windows
    clock-tie). If a future PR flips to <, this test catches it.
    """
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=14)
    cutoff_iso = cutoff_dt.isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xTIE", 50.0, cutoff_iso),
    )
    await db._conn.commit()
    deleted = await db.prune_score_history(keep_days=14)
    assert deleted == 1


async def test_prune_volume_snapshots_keeps_recent(db):
    now = datetime.now(timezone.utc)
    recent = (now - timedelta(days=5)).isoformat()
    old = (now - timedelta(days=20)).isoformat()
    await db._conn.execute(
        "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) VALUES (?, ?, ?)",
        ("0xRECENT", 100000.0, recent),
    )
    await db._conn.execute(
        "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) VALUES (?, ?, ?)",
        ("0xOLD", 100000.0, old),
    )
    await db._conn.commit()

    deleted = await db.prune_volume_snapshots(keep_days=14)

    assert deleted == 1
    cur = await db._conn.execute("SELECT contract_address FROM volume_snapshots")
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["0xRECENT"]


async def test_prune_volume_snapshots_empty_table_returns_zero(db):
    deleted = await db.prune_volume_snapshots(keep_days=14)
    assert deleted == 0


async def test_prune_volume_snapshots_tie_on_cutoff_deletes(db):
    """V6 fold: parity with prune_score_history tie-on-cutoff test.

    Locks in <= semantic on volume side. Without this, a future PR could
    flip score to keep <= while flipping volume to <, and only the score
    test would catch it.
    """
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=14)
    cutoff_iso = cutoff_dt.isoformat()
    await db._conn.execute(
        "INSERT INTO volume_snapshots (contract_address, volume_24h_usd, scanned_at) VALUES (?, ?, ?)",
        ("0xTIE", 100000.0, cutoff_iso),
    )
    await db._conn.commit()
    deleted = await db.prune_volume_snapshots(keep_days=14)
    assert deleted == 1


async def test_prune_score_history_future_dated_rows_survive_keep_days_zero(db):
    """V6 fold: keep_days=0 must NOT delete future-dated rows (clock skew /
    test seed). cutoff = now, predicate is scanned_at <= cutoff — future
    scanned_at > now > cutoff → must survive.
    """
    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=1)).isoformat()
    past = (now - timedelta(seconds=1)).isoformat()
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xFUTURE", 50.0, future),
    )
    await db._conn.execute(
        "INSERT INTO score_history (contract_address, score, scanned_at) VALUES (?, ?, ?)",
        ("0xPAST", 50.0, past),
    )
    await db._conn.commit()

    deleted = await db.prune_score_history(keep_days=0)

    assert deleted == 1
    cur = await db._conn.execute("SELECT contract_address FROM score_history")
    rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["0xFUTURE"]


@pytest.mark.parametrize(
    "table,column,index",
    [
        ("volume_spikes", "detected_at", "idx_volume_spikes_detected_at"),
        ("momentum_7d", "detected_at", "idx_momentum_7d_detected_at"),
        ("trending_snapshots", "snapshot_at", "idx_trending_snapshots_snapshot_at"),
        ("learn_logs", "created_at", "idx_learn_logs_created_at"),
        ("holder_snapshots", "scanned_at", "idx_holder_snapshots_scanned_at"),
    ],
)
async def test_narrative_table_prune_uses_new_index(db, table, column, index):
    """BL-NEW-NARRATIVE-PRUNE-SCOPE-EXPANSION (V9 plan-review fold): each
    of the 5 new tables must have a usable single-column index for the
    prune DELETE's time-only WHERE clause."""
    cur = await db._conn.execute(
        f"EXPLAIN QUERY PLAN DELETE FROM {table} WHERE {column} <= ?",
        ("2026-01-01T00:00:00+00:00",),
    )
    plan = await cur.fetchall()
    plan_str = " ".join(str(row[3]) for row in plan)
    assert index in plan_str, f"{index} not used: {plan_str}"


async def test_migration_idempotent_on_second_initialize(tmp_path):
    """V6 fold: migration idempotency via paper_migrations row check.

    A future refactor breaking the SELECT 1 FROM paper_migrations guard
    would cause CREATE INDEX to re-run on every restart, blocking dashboard
    reads for 30-60s. This test calls initialize() twice on the same DB
    and asserts the second pass skips the CREATE INDEX execution.
    """
    db = Database(str(tmp_path / "idempotent.db"))
    await db.initialize()
    # Snapshot paper_migrations rows after first init
    cur = await db._conn.execute("SELECT name FROM paper_migrations")
    after_first = {row[0] for row in await cur.fetchall()}
    await db.close()

    # Re-init — second pass should skip the score/volume migrations
    db2 = Database(str(tmp_path / "idempotent.db"))
    await db2.initialize()
    cur = await db2._conn.execute("SELECT name FROM paper_migrations")
    after_second = {row[0] for row in await cur.fetchall()}
    await db2.close()

    assert "score_history_scanned_at_idx_v1" in after_first
    assert "volume_snapshots_scanned_at_idx_v1" in after_first
    assert after_first == after_second  # no rows added on re-init


async def test_upsert_and_retrieve(db, token_factory):
    token = token_factory(quant_score=75)
    await db.upsert_candidate(token)
    candidates = await db.get_candidates_above_score(60)
    assert len(candidates) == 1
    assert candidates[0]["contract_address"] == "0xtest"
    assert candidates[0]["quant_score"] == 75


async def test_upsert_updates_existing(db, token_factory):
    token = token_factory()
    await db.upsert_candidate(token)
    token2 = token_factory(volume_24h_usd=99999.0, quant_score=80)
    await db.upsert_candidate(token2)
    candidates = await db.get_candidates_above_score(0)
    assert len(candidates) == 1
    assert candidates[0]["volume_24h_usd"] == 99999.0


async def test_get_candidates_above_score_filters(db, token_factory):
    await db.upsert_candidate(token_factory(contract_address="0xa", quant_score=50))
    await db.upsert_candidate(token_factory(contract_address="0xb", quant_score=70))
    await db.upsert_candidate(token_factory(contract_address="0xc", quant_score=None))
    results = await db.get_candidates_above_score(60)
    assert len(results) == 1
    assert results[0]["contract_address"] == "0xb"


async def test_log_alert_and_daily_count(db):
    await db.log_alert("0xalert", "solana", 85.0)
    await db.log_alert("0xalert2", "ethereum", 72.0)
    count = await db.get_daily_alert_count()
    assert count == 2


async def test_log_mirofish_job_and_daily_count(db):
    await db.log_mirofish_job("0xjob1")
    await db.log_mirofish_job("0xjob2")
    await db.log_mirofish_job("0xjob3")
    count = await db.get_daily_mirofish_count()
    assert count == 3


async def test_get_recent_alerts(db):
    await db.log_alert("0xrecent", "solana", 90.0)
    alerts = await db.get_recent_alerts(days=30)
    assert len(alerts) == 1
    assert alerts[0]["contract_address"] == "0xrecent"


async def test_signals_fired_persisted(db, token_factory):
    """signals_fired list is stored as JSON and retrievable."""
    token = token_factory(
        quant_score=75,
        signals_fired=["vol_liq_ratio", "holder_growth", "market_cap_range"],
    )
    await db.upsert_candidate(token)
    candidates = await db.get_candidates_above_score(0)
    assert len(candidates) == 1
    import json

    signals = json.loads(candidates[0]["signals_fired"])
    assert signals == ["vol_liq_ratio", "holder_growth", "market_cap_range"]


async def test_signals_fired_none(db, token_factory):
    """signals_fired=None stores as NULL."""
    token = token_factory(quant_score=50)
    await db.upsert_candidate(token)
    candidates = await db.get_candidates_above_score(0)
    assert candidates[0]["signals_fired"] is None


async def test_holder_snapshots(db):
    """Log and retrieve holder count snapshots."""
    await db.log_holder_snapshot("0xtoken", 100)
    await db.log_holder_snapshot("0xtoken", 150)

    prev = await db.get_previous_holder_count("0xtoken")
    assert prev == 150  # most recent

    # Unknown contract returns None
    unknown = await db.get_previous_holder_count("0xunknown")
    assert unknown is None


async def test_score_history(db):
    """Log and retrieve score history (newest first)."""
    await db.log_score("0xtoken", 40.0)
    await db.log_score("0xtoken", 55.0)
    await db.log_score("0xtoken", 70.0)

    scores = await db.get_recent_scores("0xtoken", limit=3)
    assert scores == [70.0, 55.0, 40.0]  # newest first

    # Limit works
    scores_2 = await db.get_recent_scores("0xtoken", limit=2)
    assert len(scores_2) == 2
    assert scores_2 == [70.0, 55.0]

    # Unknown contract returns empty
    empty = await db.get_recent_scores("0xunknown")
    assert empty == []
