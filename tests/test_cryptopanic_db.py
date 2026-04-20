"""Tests for cryptopanic_posts table + persist/prune methods."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from scout.db import Database
from scout.news.schemas import CryptoPanicPost


@pytest.fixture
async def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    await d.initialize()
    yield d
    await d.close()


def _post(post_id: int, published_at: str, title: str = "t") -> CryptoPanicPost:
    return CryptoPanicPost(
        post_id=post_id,
        title=title,
        url=f"u/{post_id}",
        published_at=published_at,
        currencies=["BTC"],
        votes_positive=1,
        votes_negative=0,
    )


async def test_initialize_is_idempotent(tmp_path):
    path = str(tmp_path / "t.db")
    d1 = Database(path)
    await d1.initialize()
    await d1.close()
    d2 = Database(path)
    await d2.initialize()
    await d2.close()


async def test_insert_cryptopanic_post(db):
    p = _post(1, "2026-04-20T10:00:00Z")
    inserted = await db.insert_cryptopanic_post(p, is_macro=False, sentiment="bullish")
    assert inserted == 1
    rows = await db.fetch_all_cryptopanic_posts()
    assert len(rows) == 1
    assert rows[0]["post_id"] == 1
    assert rows[0]["sentiment"] == "bullish"
    assert rows[0]["is_macro"] == 0
    assert json.loads(rows[0]["currencies_json"]) == ["BTC"]


async def test_insert_dup_post_id_is_idempotent(db):
    p = _post(42, "2026-04-20T10:00:00Z")
    await db.insert_cryptopanic_post(p, is_macro=False, sentiment="bullish")
    await db.insert_cryptopanic_post(p, is_macro=True, sentiment="bearish")
    rows = await db.fetch_all_cryptopanic_posts()
    assert len(rows) == 1  # second insert ignored


async def test_prune_cryptopanic_posts_keeps_recent(db):
    fresh = datetime.now(timezone.utc).isoformat()
    stale = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    await db.insert_cryptopanic_post(_post(1, fresh), is_macro=False, sentiment="neutral")
    await db.insert_cryptopanic_post(_post(2, stale), is_macro=False, sentiment="neutral")
    pruned = await db.prune_cryptopanic_posts(keep_days=7)
    assert pruned == 1
    rows = await db.fetch_all_cryptopanic_posts()
    assert len(rows) == 1
    assert rows[0]["post_id"] == 1


async def test_prune_cryptopanic_posts_empty_table_returns_zero(db):
    """Prune on an empty table must return 0, not error."""
    pruned = await db.prune_cryptopanic_posts(keep_days=7)
    assert pruned == 0


async def test_prune_cryptopanic_posts_keep_days_zero_deletes_all(db):
    """keep_days=0 means 'keep nothing older than now' — all rows delete."""
    fresh = datetime.now(timezone.utc).isoformat()
    await db.insert_cryptopanic_post(_post(1, fresh), is_macro=False, sentiment="neutral")
    await db.insert_cryptopanic_post(_post(2, fresh), is_macro=False, sentiment="neutral")
    pruned = await db.prune_cryptopanic_posts(keep_days=0)
    assert pruned == 2
    rows = await db.fetch_all_cryptopanic_posts()
    assert rows == []
