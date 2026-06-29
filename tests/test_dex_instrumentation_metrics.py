"""C5 — coverage metrics: dex_resolution_health + dex_measurable_cohort_size.

Both are computed over CG-listed DEX tokens only (B1 survivorship caveat):
- resolution_health = covered / listed_dex  (substrate health)
- measurable_cohort_size = covered          (analysis readiness)
where listed_dex = DEX contracts with a resolved coin_id, and covered = those
that ALSO have an entry-mcap row AND >=1 coin_id-keyed outcome-surface match.
"""

import pytest

from scout.db import Database

SOL = "9cRCn9rGT8V2imeM2BaKs13yhMEais3ruM3rPvTGpump"   # covered
BASE = "0xae3e205c3235c9c3a8a8d0fa72cd3cf5f7e9c8b1"     # listed, no entry
SOL2 = "So11111111111111111111111111111111111111112"   # listed, no outcome
SOL3 = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"   # unresolved (coin_id NULL)
CG = "wrapped-bitcoin"                                  # CG-native -> excluded


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "i5.db")
    await d.initialize()
    yield d
    await d.close()


async def _map(db, addr, chain, coin_id):
    await db._conn.execute(
        "INSERT INTO contract_coin_map "
        "(contract_address, chain, coin_id, resolved_at, source, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (addr, chain, coin_id, "2026-06-29T00:00:00+00:00", "platforms", "high"),
    )
    await db._conn.commit()


async def _gainer(db, coin_id):
    await db._conn.execute(
        "INSERT INTO gainers_snapshots "
        "(coin_id, symbol, name, price_change_24h, market_cap, snapshot_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (coin_id, coin_id[:5], coin_id, 100.0, 1_000_000.0, "2026-06-29T00:00:00+00:00"),
    )
    await db._conn.commit()


@pytest.fixture
async def seeded(db):
    # SOL: resolved + entry + outcome -> covered
    await _map(db, SOL, "solana", "the-black-bull")
    await db.record_entry_mcap(SOL, "solana", "2026-06-24T00:00:00+00:00", 189931.0, 1.0, 9.0)
    await _gainer(db, "the-black-bull")
    # BASE: resolved + outcome but NO entry -> listed, not covered
    await _map(db, BASE, "base", "base-coin")
    await _gainer(db, "base-coin")
    # SOL2: resolved + entry but NO outcome -> listed, not covered
    await _map(db, SOL2, "solana", "lonely-coin")
    await db.record_entry_mcap(SOL2, "solana", "2026-06-24T00:00:00+00:00", 5000.0, 1.0, 1.0)
    # CG-native: fully joinable but excluded by classifier
    await _map(db, CG, "coingecko", "wrapped-bitcoin")
    await _gainer(db, "wrapped-bitcoin")
    # SOL3: unresolved (coin_id NULL) -> not listed
    await _map(db, SOL3, "solana", None)
    return db


async def test_resolution_health_is_covered_over_listed(seeded):
    m = await seeded.compute_dex_coverage_metrics()
    # listed_dex = SOL, BASE, SOL2 (CG excluded, SOL3 unresolved) = 3; covered = SOL = 1
    assert m["listed_dex"] == 3
    assert m["covered"] == 1
    assert m["dex_resolution_health"] == pytest.approx(1 / 3)


async def test_measurable_cohort_size_is_covered_count(seeded):
    m = await seeded.compute_dex_coverage_metrics()
    assert m["dex_measurable_cohort_size"] == 1


async def test_empty_db_is_zero_not_crash(db):
    m = await db.compute_dex_coverage_metrics()
    assert m["listed_dex"] == 0
    assert m["dex_resolution_health"] == 0.0
    assert m["dex_measurable_cohort_size"] == 0
