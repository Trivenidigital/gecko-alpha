"""I2 — entry_mcap_snapshots writer (C3). Observe-only.

Semantics (spec C2): record the *earliest DEX-side* entry mcap, write-once,
DEX-mcap preferred over CG-side zero/placeholder, hold the slot open until a
non-zero DEX mcap is seen, and never pruned.
"""

import pytest

from scout.db import Database

SOL = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"  # is_dex -> solana


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "i2.db")
    await d.initialize()
    yield d
    await d.close()


async def _row(db, addr):
    cur = await db._conn.execute(
        "SELECT chain, first_seen_at, mcap_usd_at_entry, captured_at "
        "FROM entry_mcap_snapshots WHERE contract_address = ?",
        (addr,),
    )
    return await cur.fetchone()


async def test_records_dex_entry_mcap_finalized_when_positive(db):
    await db.record_entry_mcap(SOL, "solana", "2026-06-24T07:23:00+00:00", 189931.0, 36353.0, 9.0)
    row = await _row(db, SOL)
    assert row is not None
    assert row["chain"] == "solana"
    assert row["mcap_usd_at_entry"] == 189931.0
    assert row["captured_at"] is not None  # finalized


async def test_write_once_keeps_earliest_and_first_finalized(db):
    await db.record_entry_mcap(SOL, "solana", "2026-06-24T07:23:00+00:00", 189931.0, 36353.0, 9.0)
    # later, larger mcap must NOT overwrite the finalized earliest entry
    await db.record_entry_mcap(SOL, "solana", "2026-06-27T00:00:00+00:00", 38000000.0, 0.0, 12.0)
    row = await _row(db, SOL)
    assert row["mcap_usd_at_entry"] == 189931.0
    assert row["first_seen_at"] == "2026-06-24T07:23:00+00:00"


async def test_zero_mcap_holds_slot_open_then_finalizes_with_earliest(db):
    # first sighting is a zero/placeholder -> held open (captured_at null)
    await db.record_entry_mcap(SOL, "solana", "2026-06-17T01:05:00+00:00", 0.0, 0.0, 2.0)
    row = await _row(db, SOL)
    assert row is not None
    assert row["mcap_usd_at_entry"] is None
    assert row["captured_at"] is None
    # later non-zero DEX mcap finalizes, preserving the earliest first_seen
    await db.record_entry_mcap(SOL, "solana", "2026-06-24T07:23:00+00:00", 189931.0, 36353.0, 9.0)
    row = await _row(db, SOL)
    assert row["mcap_usd_at_entry"] == 189931.0
    assert row["captured_at"] is not None
    assert row["first_seen_at"] == "2026-06-17T01:05:00+00:00"


async def test_cg_native_contract_is_skipped(db):
    await db.record_entry_mcap("the-black-bull", "coingecko", "2026-06-27T14:39:00+00:00", 0.0, 0.0, 0.0)
    assert await _row(db, "the-black-bull") is None


async def test_entry_mcap_survives_candidate_prune(db):
    # entry_mcap_snapshots must NOT be pruned even when its first_seen is old
    await db.record_entry_mcap(SOL, "solana", "2020-01-01T00:00:00+00:00", 1000.0, 10.0, 1.0)
    await db.prune_old_candidates(keep_days=7)
    assert await _row(db, SOL) is not None
