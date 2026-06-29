"""I3 — raw txns_h1_buys capture (C4): GeckoTerminal parse + writer. Observe-only.

Stores raw absolute buy/sell counts + source + timestamp; deltas are computed in
analysis, never here. Captured-not-scored.
"""

import pytest

from scout.db import Database
from scout.models import CandidateToken

SOL = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


def _gt_pool(with_txns: bool):
    attrs = {
        "name": "ANSEM / SOL",
        "fdv_usd": "189931",
        "reserve_in_usd": "36353",
        "volume_usd": {"h24": "84195"},
    }
    if with_txns:
        attrs["transactions"] = {"h1": {"buys": 120, "sells": 30}}
    return {
        "attributes": attrs,
        "relationships": {"base_token": {"data": {"id": f"solana_{SOL}"}}},
    }


def test_from_geckoterminal_parses_h1_txns():
    t = CandidateToken.from_geckoterminal(_gt_pool(with_txns=True), "solana")
    assert t.txns_h1_buys == 120
    assert t.txns_h1_sells == 30


def test_from_geckoterminal_missing_txns_is_none():
    t = CandidateToken.from_geckoterminal(_gt_pool(with_txns=False), "solana")
    assert t.txns_h1_buys is None
    assert t.txns_h1_sells is None


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "i3.db")
    await d.initialize()
    yield d
    await d.close()


async def _rows(db, addr):
    cur = await db._conn.execute(
        "SELECT txns_h1_buys, txns_h1_sells, source FROM txns_h1_buys_snapshots "
        "WHERE contract_address = ? ORDER BY id",
        (addr,),
    )
    return await cur.fetchall()


async def test_log_txns_snapshot_writes_raw_with_source(db):
    await db.log_txns_snapshot(SOL, 120, 30, "dexscreener")
    rows = await _rows(db, SOL)
    assert len(rows) == 1
    assert rows[0]["txns_h1_buys"] == 120
    assert rows[0]["txns_h1_sells"] == 30
    assert rows[0]["source"] == "dexscreener"


async def test_log_txns_snapshot_skips_when_both_none(db):
    # no source provided buy/sell counts -> no row (visible to non-null watchdog)
    await db.log_txns_snapshot(SOL, None, None, "dexscreener")
    assert await _rows(db, SOL) == []


async def test_log_txns_snapshot_is_append_only_timeseries(db):
    await db.log_txns_snapshot(SOL, 100, 20, "dexscreener")
    await db.log_txns_snapshot(SOL, 150, 25, "geckoterminal")
    rows = await _rows(db, SOL)
    assert len(rows) == 2
    assert [r["source"] for r in rows] == ["dexscreener", "geckoterminal"]
