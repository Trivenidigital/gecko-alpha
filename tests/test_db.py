"""Tests for scout.db module."""

import pytest

from scout.db import Database
from scout.models import CandidateToken


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


def _make_token(**overrides) -> CandidateToken:
    defaults = dict(
        contract_address="0xtest",
        chain="solana",
        token_name="Test",
        ticker="TST",
        token_age_days=1.0,
        market_cap_usd=50000.0,
        liquidity_usd=10000.0,
        volume_24h_usd=80000.0,
        holder_count=100,
        holder_growth_1h=20,
    )
    defaults.update(overrides)
    return CandidateToken(**defaults)


async def test_upsert_and_retrieve(db):
    token = _make_token(quant_score=75)
    await db.upsert_candidate(token)
    candidates = await db.get_candidates_above_score(60)
    assert len(candidates) == 1
    assert candidates[0]["contract_address"] == "0xtest"
    assert candidates[0]["quant_score"] == 75


async def test_upsert_updates_existing(db):
    token = _make_token()
    await db.upsert_candidate(token)
    token2 = _make_token(volume_24h_usd=99999.0, quant_score=80)
    await db.upsert_candidate(token2)
    candidates = await db.get_candidates_above_score(0)
    assert len(candidates) == 1
    assert candidates[0]["volume_24h_usd"] == 99999.0


async def test_get_candidates_above_score_filters(db):
    await db.upsert_candidate(_make_token(contract_address="0xa", quant_score=50))
    await db.upsert_candidate(_make_token(contract_address="0xb", quant_score=70))
    await db.upsert_candidate(_make_token(contract_address="0xc", quant_score=None))
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
