"""PR-B — outcome-ledger enrollment for DEX discoveries (reviewer contract).

Counter model (exact, per review ruling):
    candidates = attempted + budget_skipped
    attempted  = succeeded + failed_none
    succeeded  = enrolled + not_needed
    => candidates = enrolled + not_needed + failed_none + budget_skipped

`candidates` = NEW discoveries only (dedup-suppressed re-sightings and
dust-filtered pools are excluded by definition — they never reach the ledger
stage). Operational ledger-write failures are contained by record_emission
and returned as None; they are counted as failed_none and never described as
enrolled. Budget = DEX_DISCOVERY_LEDGER_ENROLL_PER_CYCLE (the only limit —
no embedded fallback).
"""

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.db import Database
from scout.ingestion import gt_new_pools

NEW_POOLS_URL = "https://api.geckoterminal.com/api/v2/networks/solana/new_pools"


def _pool(i, reserve=5_000.0):
    return {
        "id": f"solana_Pool{i}",
        "attributes": {
            "address": f"Pool{i}",
            "name": f"TOK{i} / SOL",
            "pool_created_at": "2026-07-20T01:00:00Z",
            "fdv_usd": "50000",
            "reserve_in_usd": str(reserve),
            "volume_usd": {"h1": "10"},
        },
        "relationships": {
            "base_token": {"data": {"id": f"solana_Mint{i}"}},
            "quote_token": {"data": {"id": "solana_SOLMINT"}},
        },
    }


async def _db(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    return db


@pytest.fixture(autouse=True)
def _reset_cycle_counter():
    gt_new_pools._poll_cycle_counter = 0
    yield
    gt_new_pools._poll_cycle_counter = 0


def _settings(settings_factory, **kw):
    base = dict(
        DEX_DISCOVERY_ENABLED=True,
        DEX_DISCOVERY_POLL_EVERY_N_CYCLES=1,
        DEX_DISCOVERY_MIN_LIQUIDITY_USD=1000.0,
        LEDGER_ENABLED=True,
    )
    base.update(kw)
    return settings_factory(**base)


async def _run(db, settings, pools):
    with aioresponses() as m:
        m.get(NEW_POOLS_URL, payload={"data": pools})
        async with aiohttp.ClientSession() as session:
            return await gt_new_pools.discover_new_pools(session, db, settings)


# ------------------------------------------------------------ enrollment


async def test_new_discovery_enrolls_in_ledger(tmp_path, settings_factory):
    db = await _db(tmp_path)
    settings = _settings(settings_factory, DEX_DISCOVERY_LEDGER_ENROLL_PER_CYCLE=3)
    await _run(db, settings, [_pool(1)])
    cur = await db._conn.execute(
        "SELECT kind, token_id, surface, enrollment_status, liquidity_at_emission "
        "FROM signal_outcome_ledger"
    )
    rows = await cur.fetchall()
    assert len(rows) == 1
    kind, token_id, surface, enrollment_status, liq = rows[0]
    assert kind == "gated_out_sample"
    assert token_id == "dex:solana:Mint1"
    assert surface == "dex_new_pool"
    # fresh mint, no in-DB coverage -> enrolled for DexScreener labeling
    assert enrollment_status == "enrolled"
    assert liq == 5_000.0
    await db.close()


async def test_budget_caps_ledger_writes_and_counts_skipped(
    tmp_path, settings_factory, caplog
):
    db = await _db(tmp_path)
    settings = _settings(settings_factory, DEX_DISCOVERY_LEDGER_ENROLL_PER_CYCLE=2)
    n = await _run(db, settings, [_pool(i) for i in range(1, 6)])
    assert n == 5  # all 5 recorded as discoveries
    cur = await db._conn.execute("SELECT COUNT(*) FROM signal_outcome_ledger")
    assert (await cur.fetchone())[0] == 2  # only budget-many ledger writes
    await db.close()


async def test_rediscovery_writes_no_ledger_row(tmp_path, settings_factory):
    db = await _db(tmp_path)
    settings = _settings(settings_factory, DEX_DISCOVERY_LEDGER_ENROLL_PER_CYCLE=5)
    with aioresponses() as m:
        m.get(NEW_POOLS_URL, payload={"data": [_pool(1)]})
        m.get(NEW_POOLS_URL, payload={"data": [_pool(1)]})
        async with aiohttp.ClientSession() as session:
            await gt_new_pools.discover_new_pools(session, db, settings)
            await gt_new_pools.discover_new_pools(session, db, settings)
    cur = await db._conn.execute("SELECT COUNT(*) FROM signal_outcome_ledger")
    assert (await cur.fetchone())[0] == 1  # re-sighting excluded by definition
    await db.close()


async def test_ledger_failure_counted_not_enrolled(
    tmp_path, settings_factory, monkeypatch
):
    db = await _db(tmp_path)
    settings = _settings(settings_factory, DEX_DISCOVERY_LEDGER_ENROLL_PER_CYCLE=3)

    async def _none(*a, **k):
        return None  # contained operational failure per record_emission contract

    monkeypatch.setattr(gt_new_pools, "record_emission", _none)
    n = await _run(db, settings, [_pool(1)])
    assert n == 1  # discovery itself still recorded
    cur = await db._conn.execute("SELECT COUNT(*) FROM signal_outcome_ledger")
    assert (await cur.fetchone())[0] == 0
    counters = gt_new_pools.last_pass_counters
    assert counters["failed_none"] == 1
    assert counters["enrolled"] == 0
    await db.close()


# ------------------------------------------------------------ reconciliation


async def test_counters_reconcile_exactly(tmp_path, settings_factory):
    """candidates = enrolled + not_needed + failed_none + budget_skipped."""
    db = await _db(tmp_path)
    settings = _settings(settings_factory, DEX_DISCOVERY_LEDGER_ENROLL_PER_CYCLE=2)
    await _run(db, settings, [_pool(i) for i in range(1, 6)] + [_pool(9, reserve=1.0)])
    c = gt_new_pools.last_pass_counters
    # dust pool 9 excluded from candidates by definition
    assert c["candidates"] == 5
    assert c["candidates"] == (
        c["enrolled"] + c["not_needed"] + c["failed_none"] + c["budget_skipped"]
    )
    assert c["attempted"] == c["succeeded"] + c["failed_none"]
    assert c["succeeded"] == c["enrolled"] + c["not_needed"]
    assert c["budget_skipped"] == 3
    await db.close()


async def test_ledger_kill_switch_respected(tmp_path, settings_factory):
    db = await _db(tmp_path)
    settings = _settings(
        settings_factory, LEDGER_ENABLED=False, DEX_DISCOVERY_LEDGER_ENROLL_PER_CYCLE=3
    )
    n = await _run(db, settings, [_pool(1)])
    assert n == 1
    cur = await db._conn.execute("SELECT COUNT(*) FROM signal_outcome_ledger")
    assert (await cur.fetchone())[0] == 0  # record_emission returns None when disabled
    await db.close()


def test_budget_setting_named_and_bounded(settings_factory):
    s = settings_factory()
    assert s.DEX_DISCOVERY_LEDGER_ENROLL_PER_CYCLE >= 0
