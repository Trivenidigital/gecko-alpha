"""RH/Pons curve-launch collector (design_rh_pons_discovery_delta_2026_09_13).

Observe-only + inert-by-default: fixtures here are SYNTHETIC and
provenance-tagged `fixture:source_derived` — they encode the source-derived
event layouts (github.com/ponsmcp/pons-mcp, 2026-09-13) and are NOT
onchain-verified. Nothing in this file constitutes a live integration check,
and poll_once must keep refusing live collection until the deployment
registry carries an 'onchain_verified' entry.
"""

import json

import aiohttp
import pytest
from aioresponses import aioresponses
from eth_utils import keccak

from scout.db import Database
from scout.ingestion import rh_pons
from scout.ingestion.rh_pons import (
    PONS_DEPLOYMENTS,
    TOPIC_CURVE_BUY,
    TOPIC_POOL_GRADUATED,
    TOPIC_TOKEN_LAUNCHED,
    PonsDeployment,
    active_deployment,
    collect_from_logs,
    execution_eligibility,
)
from scout.safety import is_safe_strict

DEP = PONS_DEPLOYMENTS[0]  # pons_v2, source_derived_unverified

TOKEN = "0x" + "11" * 20
CURVE = "0x" + "22" * 20
DEPLOYER = "0x" + "33" * 20
PAIR = "0x" + "44" * 20
BUYER = "0x" + "55" * 20


def _word_addr(addr: str) -> str:
    return "0" * 24 + addr[2:].lower()


def _word_int(value: int) -> str:
    return f"{value:064x}"


def _log(
    *,
    address,
    topics,
    data="0x",
    tx="0x" + "aa" * 32,
    log_index=0,
    block=100,
    block_hash="0x" + "bb" * 32,
    timestamp=None,
    removed=False,
):
    entry = {
        "address": address,
        "topics": topics,
        "data": data,
        "transactionHash": tx,
        "logIndex": hex(log_index),
        "blockNumber": hex(block),
        "blockHash": block_hash,
    }
    if timestamp is not None:
        entry["blockTimestamp"] = timestamp
    if removed:
        entry["removed"] = True
    return entry


def _launch_log(**kw):
    return _log(
        address=DEP.factory,
        topics=[
            TOPIC_TOKEN_LAUNCHED,
            "0x" + _word_addr(TOKEN),
            "0x" + _word_addr(CURVE),
            "0x" + _word_addr(DEPLOYER),
        ],
        data="0x" + _word_addr(PAIR) + _word_int(7) + _word_int(10**18),
        **kw,
    )


def _buy_log(**kw):
    return _log(
        address=CURVE,
        topics=[
            TOPIC_CURVE_BUY,
            "0x" + _word_addr(BUYER),
            "0x" + _word_addr(BUYER),
        ],
        data="0x"
        + _word_int(5 * 10**17)
        + _word_int(10**21)
        + _word_int(10**15)
        + _word_int(0),
        **kw,
    )


def _graduated_log(**kw):
    return _log(
        address=DEP.factory,
        topics=[TOPIC_POOL_GRADUATED, "0x" + _word_addr(TOKEN)],
        data="0x" + _word_int(42) + _word_int(10**20) + _word_int(3 * 10**18),
        **kw,
    )


async def _db(tmp_path):
    db = Database(tmp_path / "t.db")
    await db.initialize()
    return db


async def _collect(logs, db, settings):
    return await collect_from_logs(
        logs,
        db,
        settings,
        source="fixture:test_rh_pons_collector",
        provenance="fixture:source_derived",
        deployment=DEP,
    )


@pytest.fixture(autouse=True)
def _reset_cycle_counter():
    rh_pons._poll_cycle_counter = 0
    yield
    rh_pons._poll_cycle_counter = 0


# ------------------------------------------------------------- topic hygiene


def test_topic_derivation_matches_known_keccak_vector():
    # The universal ERC-20 Transfer topic0 proves the derivation path is real
    # keccak-256, so no Pons topic in the registry is a hand-typed guess.
    assert (
        rh_pons._topic("Transfer(address,address,uint256)")
        == "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
    )
    assert TOPIC_TOKEN_LAUNCHED == "0x" + keccak(text=rh_pons.SIG_TOKEN_LAUNCHED).hex()


def test_registry_is_inert_no_deployment_is_collectable():
    assert active_deployment() is None
    assert all(not d.collectable for d in PONS_DEPLOYMENTS)


# ---------------------------------------------------------------- migration


async def test_migration_creates_tables_and_version(tmp_path):
    db = await _db(tmp_path)
    cur = await db._conn.execute(
        "SELECT description FROM schema_version WHERE version=20260913"
    )
    assert (await cur.fetchone())[0] == "rh_pons_discovery_v1"
    for table in ("curve_launch_discoveries", "curve_launch_events"):
        cur = await db._conn.execute(f"SELECT COUNT(*) FROM {table}")
        assert (await cur.fetchone())[0] == 0
    await db.close()


# ------------------------------------------------------------------- decode


async def test_token_launched_records_discovery_and_evidence(
    tmp_path, settings_factory
):
    db = await _db(tmp_path)
    counters = await _collect(
        [_launch_log(timestamp="2026-09-13T10:00:00+00:00")], db, settings_factory()
    )
    assert counters["new_launches"] == 1
    assert counters["recorded_events"] == 1

    launch = await db.get_curve_launch(rh_pons.ROBINHOOD_CHAIN_ID, TOKEN)
    assert launch["lifecycle_status"] == "on_curve"
    assert launch["curve_address"] == CURVE
    assert launch["deployer_address"] == DEPLOYER
    assert launch["pair_token_address"] == PAIR
    assert launch["graduation_threshold"] == str(10**18)
    assert launch["event_time"] == "2026-09-13T10:00:00+00:00"
    assert launch["execution_eligible"] == 0
    reasons = json.loads(launch["eligibility_reasons"])
    assert set(reasons) == {
        "deployment_unverified",
        "quote_asset_unapproved",
        "safety_unknown",
    }
    await db.close()


async def test_first_buy_in_launch_tx_any_input_order(tmp_path, settings_factory):
    # Same transaction carries TokenLaunched (log 5) and the sniper's
    # CurveBuy (log 7); the collector must handle them deterministically
    # even when the input list arrives reversed.
    db = await _db(tmp_path)
    tx = "0x" + "cc" * 32
    launch = _launch_log(tx=tx, log_index=5, block=200)
    buy = _buy_log(tx=tx, log_index=7, block=200)
    counters = await _collect([buy, launch], db, settings_factory())
    assert counters["recorded_events"] == 2
    cur = await db._conn.execute(
        "SELECT token_address FROM curve_launch_events WHERE event_name='curve_buy'"
    )
    # Token identity resolved through the curve discovered in the same pass.
    assert (await cur.fetchone())[0] == TOKEN
    await db.close()


async def test_duplicate_logs_dedup(tmp_path, settings_factory):
    db = await _db(tmp_path)
    log = _launch_log()
    first = await _collect([log], db, settings_factory())
    second = await _collect([log], db, settings_factory())
    assert first["recorded_events"] == 1
    assert second["recorded_events"] == 0
    assert second["duplicates"] == 1
    cur = await db._conn.execute("SELECT COUNT(*) FROM curve_launch_events")
    assert (await cur.fetchone())[0] == 1
    cur = await db._conn.execute("SELECT COUNT(*) FROM curve_launch_discoveries")
    assert (await cur.fetchone())[0] == 1
    await db.close()


async def test_unknown_factory_topic_preserved_raw(tmp_path, settings_factory):
    # LaunchSwept's layout is unresolved — a factory log with an unknown
    # topic must be preserved raw for later re-decode, never guessed at.
    db = await _db(tmp_path)
    mystery = _log(
        address=DEP.factory,
        topics=["0x" + "e0" * 32],
        data="0x" + _word_int(123),
    )
    counters = await _collect([mystery], db, settings_factory())
    assert counters["unknown_events"] == 1
    cur = await db._conn.execute(
        "SELECT payload_json FROM curve_launch_events "
        "WHERE event_name='unknown_factory_event'"
    )
    payload = json.loads((await cur.fetchone())[0])
    assert payload["raw"]["topics"] == ["0x" + "e0" * 32]
    assert payload["raw"]["data"] == "0x" + _word_int(123)
    await db.close()


async def test_non_registry_address_unknown_topic_not_recorded(
    tmp_path, settings_factory
):
    db = await _db(tmp_path)
    stranger = _log(address="0x" + "99" * 20, topics=["0x" + "e1" * 32])
    counters = await _collect([stranger], db, settings_factory())
    assert counters["undecodable"] == 1
    cur = await db._conn.execute("SELECT COUNT(*) FROM curve_launch_events")
    assert (await cur.fetchone())[0] == 0
    await db.close()


# ------------------------------------------------------------------- reorgs


async def test_removed_log_appends_marker_only(tmp_path, settings_factory):
    db = await _db(tmp_path)
    counters = await _collect([_launch_log(removed=True)], db, settings_factory())
    assert counters["reorg_markers"] == 1
    assert counters["recorded_events"] == 0
    cur = await db._conn.execute("SELECT event_name FROM curve_launch_events")
    assert [r[0] for r in await cur.fetchall()] == ["reorg_removed"]
    await db.close()


async def test_reorged_block_hash_appends_replaced_marker(tmp_path, settings_factory):
    db = await _db(tmp_path)
    await _collect([_launch_log(block_hash="0x" + "01" * 32)], db, settings_factory())
    counters = await _collect(
        [_launch_log(block_hash="0x" + "02" * 32)], db, settings_factory()
    )
    assert counters["reorg_markers"] == 1
    cur = await db._conn.execute(
        "SELECT event_name, block_hash FROM curve_launch_events ORDER BY id"
    )
    rows = await cur.fetchall()
    names = [r[0] for r in rows]
    assert names == ["token_launched", "reorg_replaced", "token_launched"]
    # Evidence is append-only: the original observation row still exists.
    assert rows[0][1] == "0x" + "01" * 32
    assert rows[2][1] == "0x" + "02" * 32
    await db.close()


# ---------------------------------------------------------------- lifecycle


async def test_graduation_advances_lifecycle_forward_only(tmp_path, settings_factory):
    db = await _db(tmp_path)
    settings = settings_factory()
    await _collect([_launch_log(block=100)], db, settings)
    await _collect([_graduated_log(block=150, tx="0x" + "dd" * 32)], db, settings)
    launch = await db.get_curve_launch(rh_pons.ROBINHOOD_CHAIN_ID, TOKEN)
    assert launch["lifecycle_status"] == "on_v4"
    # Backward projection is refused.
    assert not await rh_pons.advance_lifecycle(
        db, rh_pons.ROBINHOOD_CHAIN_ID, TOKEN, "on_curve"
    )
    launch = await db.get_curve_launch(rh_pons.ROBINHOOD_CHAIN_ID, TOKEN)
    assert launch["lifecycle_status"] == "on_v4"
    await db.close()


async def test_graduation_without_seen_birth_creates_row(tmp_path, settings_factory):
    db = await _db(tmp_path)
    counters = await _collect([_graduated_log()], db, settings_factory())
    assert counters["new_launches"] == 1
    launch = await db.get_curve_launch(rh_pons.ROBINHOOD_CHAIN_ID, TOKEN)
    assert launch["lifecycle_status"] == "on_v4"
    assert launch["curve_address"] is None  # never fabricated
    await db.close()


async def test_backfilled_launch_fills_nulls_keeps_earlier_first_seen(
    tmp_path, settings_factory
):
    db = await _db(tmp_path)
    settings = settings_factory()
    await _collect([_graduated_log(block=150)], db, settings)
    first = await db.get_curve_launch(rh_pons.ROBINHOOD_CHAIN_ID, TOKEN)
    await _collect([_launch_log(block=100, tx="0x" + "ee" * 32)], db, settings)
    launch = await db.get_curve_launch(rh_pons.ROBINHOOD_CHAIN_ID, TOKEN)
    assert launch["curve_address"] == CURVE  # NULL filled by the backfill
    assert launch["first_seen_at"] == first["first_seen_at"]  # earliest kept
    assert launch["lifecycle_status"] == "on_v4"  # never regressed
    await db.close()


# ----------------------------------------------------------- resume cursor


async def test_resume_cursor_is_max_observed_block(tmp_path, settings_factory):
    db = await _db(tmp_path)
    assert await db.max_curve_event_block(rh_pons.ROBINHOOD_CHAIN_ID) is None
    await _collect([_launch_log(block=100)], db, settings_factory())
    await _collect(
        [_graduated_log(block=175, tx="0x" + "df" * 32)], db, settings_factory()
    )
    assert await db.max_curve_event_block(rh_pons.ROBINHOOD_CHAIN_ID) == 175
    await db.close()


# ------------------------------------------------------ poll_once gating


async def test_poll_once_flag_off_no_http(tmp_path, settings_factory):
    db = await _db(tmp_path)
    settings = settings_factory(RH_PONS_COLLECTOR_ENABLED=False)
    with aioresponses():  # any HTTP call would raise (no mocks registered)
        async with aiohttp.ClientSession() as session:
            assert await rh_pons.poll_once(session, db, settings) == 0
    await db.close()


async def test_poll_once_refuses_unverified_deployment(tmp_path, settings_factory):
    # Flag ON and URL configured — but the registry has no onchain_verified
    # deployment, so the collector must still refuse without any HTTP.
    db = await _db(tmp_path)
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_RPC_URL="https://rpc.example.invalid",
        RH_PONS_POLL_EVERY_N_CYCLES=1,
    )
    with aioresponses():
        async with aiohttp.ClientSession() as session:
            assert await rh_pons.poll_once(session, db, settings) == 0
    await db.close()


def _verified_dep() -> PonsDeployment:
    # Synthetic TEST-ONLY registry state: same source-derived address, marked
    # verified so the transport/backfill path is exercisable. Prod stays
    # inert because the real registry never carries this entry.
    return PonsDeployment(
        version="pons_v2",
        chain_id=DEP.chain_id,
        network=DEP.network,
        factory=DEP.factory,
        verification_status="onchain_verified",
        sources=("test-fixture",),
        deploy_block=90,
    )


async def test_poll_once_refuses_without_rpc_url(
    tmp_path, settings_factory, monkeypatch
):
    db = await _db(tmp_path)
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (_verified_dep(),))
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True, RH_PONS_POLL_EVERY_N_CYCLES=1
    )
    with aioresponses():
        async with aiohttp.ClientSession() as session:
            assert await rh_pons.poll_once(session, db, settings) == 0
    await db.close()


async def test_poll_once_transport_and_resume(tmp_path, settings_factory, monkeypatch):
    db = await _db(tmp_path)
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (_verified_dep(),))
    rpc = "https://rpc.example.invalid"
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_RPC_URL=rpc,
        RH_PONS_POLL_EVERY_N_CYCLES=1,
        RH_PONS_BACKFILL_BLOCK_SPAN=1000,
    )
    with aioresponses() as m:
        m.post(rpc, payload={"jsonrpc": "2.0", "id": 1, "result": [_launch_log()]})
        async with aiohttp.ClientSession() as session:
            recorded = await rh_pons.poll_once(session, db, settings)
        assert recorded == 1
        (first_call,) = [c for calls in m.requests.values() for c in calls]
        assert first_call.kwargs["json"]["params"][0]["fromBlock"] == hex(90)

    # Second pass resumes AFTER the highest observed block (100), not from
    # the deploy block — reconnect/backfill continuity.
    rh_pons._poll_cycle_counter = 0
    with aioresponses() as m:
        m.post(rpc, payload={"jsonrpc": "2.0", "id": 1, "result": []})
        async with aiohttp.ClientSession() as session:
            assert await rh_pons.poll_once(session, db, settings) == 0
        (second_call,) = [c for calls in m.requests.values() for c in calls]
        assert second_call.kwargs["json"]["params"][0]["fromBlock"] == hex(101)
    await db.close()


# ---------------------------------------- execution eligibility (ruling 4)


def test_eligibility_unknowns_are_ineligible():
    ok, reasons = execution_eligibility(
        deployment_verified=False, quote_asset_approved=False, safety_verdict=None
    )
    assert not ok
    assert reasons == [
        "deployment_unverified",
        "quote_asset_unapproved",
        "safety_unknown",
    ]

    ok, reasons = execution_eligibility(
        deployment_verified=True,
        quote_asset_approved=True,
        safety_verdict=(False, False),  # outage/timeout/missing-record shape
    )
    assert not ok and reasons == ["safety_unknown"]

    ok, reasons = execution_eligibility(
        deployment_verified=True,
        quote_asset_approved=True,
        safety_verdict=(False, True),  # completed adverse verdict
    )
    assert not ok and reasons == ["safety_adverse_verdict"]

    ok, reasons = execution_eligibility(
        deployment_verified=True, quote_asset_approved=True, safety_verdict=(True, True)
    )
    assert ok and reasons == []


GOPLUS_URL = "https://api.gopluslabs.io/api/v1/token_security/1"


async def _strict(contract="0x" + "ab" * 20):
    async with aiohttp.ClientSession() as session:
        return await is_safe_strict(contract, "ethereum", session)


async def test_strict_outage_maps_to_safety_unknown():
    with aioresponses() as m:
        m.get(
            f"{GOPLUS_URL}?contract_addresses=0x{'ab' * 20}",
            status=503,
        )
        verdict = await _strict()
    assert verdict == (False, False)
    ok, reasons = execution_eligibility(
        deployment_verified=True, quote_asset_approved=True, safety_verdict=verdict
    )
    assert not ok and reasons == ["safety_unknown"]


async def test_strict_timeout_maps_to_safety_unknown():
    with aioresponses() as m:
        m.get(
            f"{GOPLUS_URL}?contract_addresses=0x{'ab' * 20}",
            exception=aiohttp.ClientConnectionError("boom"),
        )
        verdict = await _strict()
    assert verdict == (False, False)
    assert not execution_eligibility(
        deployment_verified=True, quote_asset_approved=True, safety_verdict=verdict
    )[0]


async def test_strict_missing_record_maps_to_safety_unknown():
    with aioresponses() as m:
        m.get(
            f"{GOPLUS_URL}?contract_addresses=0x{'ab' * 20}",
            payload={"result": {}},
        )
        verdict = await _strict()
    assert verdict == (False, False)
    assert not execution_eligibility(
        deployment_verified=True, quote_asset_approved=True, safety_verdict=verdict
    )[0]


async def test_strict_adverse_verdict_maps_to_adverse_reason():
    contract = "0x" + "ab" * 20
    with aioresponses() as m:
        m.get(
            f"{GOPLUS_URL}?contract_addresses={contract}",
            payload={"result": {contract: {"is_honeypot": "1"}}},
        )
        verdict = await _strict(contract)
    assert verdict == (False, True)
    ok, reasons = execution_eligibility(
        deployment_verified=True, quote_asset_approved=True, safety_verdict=verdict
    )
    assert not ok and reasons == ["safety_adverse_verdict"]
