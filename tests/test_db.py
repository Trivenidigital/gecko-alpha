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
