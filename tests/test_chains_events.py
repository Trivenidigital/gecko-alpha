"""Tests for chain event emission + retention."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.chains.events import (
    emit_event,
    load_recent_events,
    prune_old_events,
    safe_emit,
)
from scout.config import Settings
from scout.db import Database


def _settings(**overrides) -> Settings:
    defaults = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    defaults.update(overrides)
    return Settings(**defaults)


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "test.db")
    await d.initialize()
    yield d
    await d.close()


async def test_emit_event_inserts_row(db):
    eid = await emit_event(
        db=db,
        token_id="0xabc",
        pipeline="memecoin",
        event_type="candidate_scored",
        event_data={"quant_score": 72, "signal_count": 3},
        source_module="scorer",
    )
    assert eid > 0
    async with db._conn.execute(
        "SELECT token_id, pipeline, event_type, event_data, source_module "
        "FROM signal_events WHERE id = ?",
        (eid,),
    ) as cur:
        row = await cur.fetchone()
    assert row["token_id"] == "0xabc"
    assert row["pipeline"] == "memecoin"
    assert row["event_type"] == "candidate_scored"
    assert json.loads(row["event_data"])["quant_score"] == 72
    assert row["source_module"] == "scorer"


async def test_emit_event_append_only(db):
    e1 = await emit_event(
        db, "0xabc", "memecoin", "candidate_scored", {"quant_score": 72}, "scorer"
    )
    e2 = await emit_event(
        db, "0xabc", "memecoin", "candidate_scored", {"quant_score": 72}, "scorer"
    )
    assert e1 != e2


async def test_load_recent_events_filters_by_window(db):
    old_ts = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("0xold", "memecoin", "candidate_scored", "{}", "scorer", old_ts),
    )
    await db._conn.commit()
    await emit_event(db, "0xnew", "memecoin", "candidate_scored", {}, "scorer")

    events = await load_recent_events(db, max_hours=24.0)
    ids = {e.token_id for e in events}
    assert "0xnew" in ids
    assert "0xold" not in ids


async def test_prune_old_events(db):
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    await db._conn.execute(
        """INSERT INTO signal_events
           (token_id, pipeline, event_type, event_data, source_module, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("0xold", "memecoin", "candidate_scored", "{}", "scorer", old_ts),
    )
    await db._conn.commit()
    await emit_event(db, "0xnew", "memecoin", "candidate_scored", {}, "scorer")

    deleted = await prune_old_events(db, retention_days=14)
    assert deleted == 1

    async with db._conn.execute("SELECT COUNT(*) FROM signal_events") as cur:
        row = await cur.fetchone()
    assert row[0] == 1


async def test_emit_event_swallows_errors_via_safe_emit(db, monkeypatch):
    """safe_emit wraps emit_event and never raises."""
    monkeypatch.setattr(
        "scout.config.get_settings",
        lambda: _settings(CHAINS_ENABLED=True),
    )

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr("scout.chains.events.emit_event", _boom)
    # Should NOT raise
    await safe_emit(db, "0xabc", "memecoin", "candidate_scored", {}, "scorer")


async def test_safe_emit_noop_when_disabled(db, monkeypatch):
    """CHAINS_ENABLED=False: safe_emit must insert ZERO rows."""
    monkeypatch.setattr(
        "scout.config.get_settings",
        lambda: _settings(CHAINS_ENABLED=False),
    )

    async with db._conn.execute("SELECT COUNT(*) FROM signal_events") as cur:
        before = (await cur.fetchone())[0]

    result = await safe_emit(
        db, "0xabc", "memecoin", "candidate_scored",
        {"quant_score": 72}, "scorer",
    )
    assert result is None

    async with db._conn.execute("SELECT COUNT(*) FROM signal_events") as cur:
        after = (await cur.fetchone())[0]
    assert before == after
