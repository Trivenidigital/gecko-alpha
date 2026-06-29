"""C2 (DB part) — contract_coin_map upsert writer. Observe-only, local-safe.

The HTTP resolver orchestration that calls these is tested separately under
aioresponses (CI), since it imports aiohttp.
"""

import pytest

from scout.db import Database

SOL = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "coinmap.db")
    await d.initialize()
    yield d
    await d.close()


async def _row(db, addr):
    cur = await db._conn.execute(
        "SELECT chain, coin_id, source, confidence FROM contract_coin_map "
        "WHERE contract_address = ?",
        (addr,),
    )
    return await cur.fetchone()


async def test_record_contract_coin_map_inserts(db):
    await db.record_contract_coin_map(SOL, "solana", "the-black-bull", "platforms", "high")
    row = await _row(db, SOL)
    assert row["coin_id"] == "the-black-bull"
    assert row["chain"] == "solana"
    assert row["source"] == "platforms"
    assert row["confidence"] == "high"


async def test_record_contract_coin_map_allows_null_coin_id_for_attempted(db):
    # negative result marker (resolution attempted, nothing found yet)
    await db.record_contract_coin_map(SOL, "solana", None, "attempted", None)
    row = await _row(db, SOL)
    assert row is not None
    assert row["coin_id"] is None


async def test_record_contract_coin_map_upserts_resolved_over_attempted(db):
    await db.record_contract_coin_map(SOL, "solana", None, "attempted", None)
    await db.record_contract_coin_map(SOL, "solana", "the-black-bull", "platforms", "high")
    row = await _row(db, SOL)
    assert row["coin_id"] == "the-black-bull"
    assert row["source"] == "platforms"


async def test_coin_id_resolved_guard(db):
    assert await db.coin_id_resolved("the-black-bull") is False
    await db.record_contract_coin_map(SOL, "solana", "the-black-bull", "platforms", "high")
    assert await db.coin_id_resolved("the-black-bull") is True
    # attempted (NULL coin_id) does not count as resolved
    await db.record_contract_coin_map("0x" + "a" * 40, "base", None, "attempted", None)
    assert await db.coin_id_resolved("nonexistent") is False
