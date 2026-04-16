"""Tests for briefing models, config, and DB table."""

import json
from datetime import datetime, timezone

import pytest

from scout.briefing.models import Briefing, BriefingData


class TestBriefingData:
    def test_defaults(self):
        bd = BriefingData()
        assert bd.fear_greed is None
        assert bd.global_market is None
        assert bd.funding_rates is None
        assert bd.liquidations is None
        assert bd.defi_tvl is None
        assert bd.news is None
        assert bd.internal is None
        assert isinstance(bd.timestamp, datetime)

    def test_with_data(self):
        bd = BriefingData(
            fear_greed={"value": 72, "classification": "Greed"},
            news=[{"title": "BTC hits 100k"}],
        )
        assert bd.fear_greed["value"] == 72
        assert len(bd.news) == 1


class TestBriefing:
    def test_create(self):
        b = Briefing(
            briefing_type="morning",
            raw_data={"fear_greed": {"value": 50}},
            synthesis="Market is neutral.",
            model_used="claude-sonnet-4-6",
            tokens_used=500,
        )
        assert b.briefing_type == "morning"
        assert b.id is None
        assert b.tokens_used == 500
        assert isinstance(b.created_at, datetime)


class TestBriefingConfig:
    def test_briefing_defaults(self, settings_factory):
        s = settings_factory()
        assert s.BRIEFING_ENABLED is False
        assert s.BRIEFING_HOURS_UTC == "6,18"
        assert s.BRIEFING_MODEL == "claude-sonnet-4-6"
        assert s.BRIEFING_TELEGRAM_ENABLED is True
        assert s.COINGLASS_API_KEY == ""

    def test_briefing_override(self, settings_factory):
        s = settings_factory(
            BRIEFING_ENABLED=True,
            BRIEFING_HOURS_UTC="0,12",
            COINGLASS_API_KEY="test-key",
        )
        assert s.BRIEFING_ENABLED is True
        assert s.BRIEFING_HOURS_UTC == "0,12"
        assert s.COINGLASS_API_KEY == "test-key"


class TestBriefingDB:
    async def test_briefings_table_created(self, tmp_path):
        from scout.db import Database

        db = Database(tmp_path / "test.db")
        await db.initialize()
        try:
            cursor = await db._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='briefings'"
            )
            row = await cursor.fetchone()
            assert row is not None
        finally:
            await db.close()

    async def test_store_and_retrieve_briefing(self, tmp_path):
        from scout.db import Database

        db = Database(tmp_path / "test.db")
        await db.initialize()
        try:
            raw = json.dumps({"fear_greed": {"value": 72}})
            bid = await db.store_briefing(
                briefing_type="morning",
                raw_data=raw,
                synthesis="Market is bullish.",
                model_used="claude-sonnet-4-6",
                tokens_used=500,
            )
            assert bid is not None
            assert bid > 0

            latest = await db.get_latest_briefing()
            assert latest is not None
            assert latest["briefing_type"] == "morning"
            assert latest["synthesis"] == "Market is bullish."
            assert latest["model_used"] == "claude-sonnet-4-6"
            assert latest["tokens_used"] == 500

            history = await db.get_briefing_history(limit=5)
            assert len(history) == 1
            assert history[0]["id"] == bid

            last_time = await db.get_last_briefing_time()
            assert last_time is not None
        finally:
            await db.close()

    async def test_get_last_briefing_time_empty(self, tmp_path):
        from scout.db import Database

        db = Database(tmp_path / "test.db")
        await db.initialize()
        try:
            last_time = await db.get_last_briefing_time()
            assert last_time is None
        finally:
            await db.close()
