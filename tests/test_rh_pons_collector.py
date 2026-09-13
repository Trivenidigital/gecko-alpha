"""RH/Pons curve-launch collector (design_rh_pons_discovery_delta_2026_09_13).

Observe-only + inert-by-default: fixtures here are SYNTHETIC and
provenance-tagged `fixture:source_derived` — they encode the source-derived
event layouts (github.com/ponsmcp/pons-mcp, 2026-09-13) and are NOT
onchain-verified. Nothing in this file constitutes a live integration check,
and poll_once must keep refusing live collection until the deployment
registry carries an 'onchain_verified' entry.
"""

import json
from dataclasses import replace

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

DEP = replace(PONS_DEPLOYMENTS[0], verification_status="source_derived_unverified")

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


def test_registry_selects_verified_curve_factory_but_defaults_off(settings_factory):
    dep = active_deployment()
    assert dep.factory == "0x7ed598bcef8bd9edd8c97a195c6d13f40801ec7e"
    assert dep.deploy_block == 26841846
    assert dep.collectable
    assert all(not d.collectable for d in PONS_DEPLOYMENTS[1:])
    assert not settings_factory().RH_PONS_COLLECTOR_ENABLED
    assert PONS_DEPLOYMENTS[1].version == "pons_direct_v3"


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


async def test_poll_once_refuses_unverified_deployment(tmp_path, settings_factory, monkeypatch):
    # Flag ON and URL configured — but the registry has no onchain_verified
    # deployment, so the collector must still refuse without any HTTP.
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (DEP,))
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
    # verified so the transport/backfill path is exercisable at block 90.
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


async def test_crash_replay_repairs_discovery(tmp_path, settings_factory, monkeypatch):
    from unittest.mock import AsyncMock

    db = await _db(tmp_path)
    writer = db.record_curve_launch_discovery
    monkeypatch.setattr(
        db,
        "record_curve_launch_discovery",
        AsyncMock(side_effect=RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError):
        await _collect([_launch_log()], db, settings_factory())
    monkeypatch.setattr(db, "record_curve_launch_discovery", writer)
    await _collect([_launch_log()], db, settings_factory())
    assert await db.get_curve_launch(DEP.chain_id, TOKEN) is not None
    await db.close()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda x: x.update(data="0x01"),
        lambda x: x.update(topics=[TOPIC_TOKEN_LAUNCHED, "0x11", "0x22", "0x33"]),
        lambda x: x.update(topics=[None]),
    ],
)
def test_malformed_layout_rejected(mutate):
    log = _launch_log()
    mutate(log)
    assert rh_pons.decode_log(log) is None


async def test_removed_graduation_reconciles(tmp_path, settings_factory):
    db = await _db(tmp_path)
    await _collect([_launch_log(), _graduated_log(log_index=1)], db, settings_factory())
    await _collect([_graduated_log(log_index=1, removed=True)], db, settings_factory())
    assert (await db.get_curve_launch(DEP.chain_id, TOKEN))[
        "lifecycle_status"
    ] == "on_curve"
    assert (
        await db.canonical_curve_event_block_hash(DEP.chain_id, "0x" + "aa" * 32, 1)
        is None
    )
    await db.close()


async def test_empty_scan_progress_and_heartbeat(
    tmp_path, settings_factory, monkeypatch
):
    from aioresponses import CallbackResult

    db = await _db(tmp_path)
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (_verified_dep(),))
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_RPC_URL="https://rpc.invalid/key/SECRET",
        RH_PONS_POLL_EVERY_N_CYCLES=1,
        RH_PONS_BACKFILL_BLOCK_SPAN=10,
    )
    scans = []

    def rpc(url, **kw):
        body = kw["json"]
        method = body["method"]
        if method == "eth_chainId":
            result = hex(DEP.chain_id)
        elif method == "eth_blockNumber":
            result = hex(105)
        elif method == "eth_getBlockByNumber":
            result = {
                "number": body["params"][0],
                "hash": "0x" + "bb" * 32,
                "timestamp": hex(1700000000),
            }
        else:
            scans.append(body["params"][0])
            result = []
        return CallbackResult(payload={"jsonrpc": "2.0", "id": 1, "result": result})

    with aioresponses() as m:
        m.post(settings.RH_PONS_RPC_URL, callback=rpc, repeat=True)
        async with aiohttp.ClientSession() as session:
            await rh_pons.poll_once(session, db, settings)
            await rh_pons.poll_once(session, db, settings)
    checkpoint = await db.get_curve_scan_checkpoint(
        DEP.chain_id, DEP.version, DEP.factory
    )
    assert checkpoint["next_block"] == 106
    assert int(scans[-1]["toBlock"], 16) == 105
    assert (await db.load_ingest_watchdog_state())["rh_pons"] == 0
    await db.close()


async def test_poll_fetches_launch_block_trades_and_stamps_clock(
    tmp_path, settings_factory, monkeypatch
):
    from aioresponses import CallbackResult

    db = await _db(tmp_path)
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (_verified_dep(),))
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_RPC_URL="https://rpc.invalid/SECRET",
        RH_PONS_POLL_EVERY_N_CYCLES=1,
    )

    def rpc(url, **kw):
        body = kw["json"]
        method = body["method"]
        if method == "eth_chainId":
            result = hex(DEP.chain_id)
        elif method == "eth_blockNumber":
            result = hex(100)
        elif method == "eth_getBlockByNumber":
            result = {
                "number": body["params"][0],
                "hash": "0x" + "bb" * 32,
                "timestamp": hex(1700000000),
            }
        else:
            address = body["params"][0]["address"]
            result = (
                [_launch_log()] if address == DEP.factory else [_buy_log(log_index=1)]
            )
        return CallbackResult(payload={"jsonrpc": "2.0", "id": 1, "result": result})

    with aioresponses() as m:
        m.post(settings.RH_PONS_RPC_URL, callback=rpc, repeat=True)
        async with aiohttp.ClientSession() as session:
            assert await rh_pons.poll_once(session, db, settings) == 2
            assert await rh_pons.poll_once(session, db, settings) == 0
    row = await db.get_curve_launch(DEP.chain_id, TOKEN)
    assert row["event_time"] == "2023-11-14T22:13:20+00:00"
    assert row["source"] == "rpc:rh_pons"
    assert not row["execution_eligible"]
    assert "deployment_unverified" not in row["eligibility_reasons"]
    cur = await db._conn.execute(
        "SELECT token_address FROM curve_launch_events WHERE event_name='curve_buy'"
    )
    assert (await cur.fetchone())[0] == TOKEN
    await db.close()


@pytest.mark.parametrize(
    "failure", ["wrong_chain", "log_error", "header_error", "malformed_log"]
)
async def test_partial_failure_never_checkpoints(
    tmp_path, settings_factory, monkeypatch, failure
):
    from aioresponses import CallbackResult

    db = await _db(tmp_path)
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (_verified_dep(),))
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_RPC_URL="https://rpc.invalid",
        RH_PONS_POLL_EVERY_N_CYCLES=1,
    )

    def rpc(url, **kw):
        body = kw["json"]
        method = body["method"]
        if method == "eth_chainId":
            result = hex(1 if failure == "wrong_chain" else DEP.chain_id)
        elif method == "eth_blockNumber":
            result = hex(100)
        elif method == "eth_getBlockByNumber":
            result = None
        elif failure == "log_error":
            return CallbackResult(payload={"error": {"message": "SECRET"}})
        elif failure == "malformed_log":
            result = [{"topics": []}]
        else:
            result = []
        return CallbackResult(payload={"result": result})

    with aioresponses() as m:
        m.post(settings.RH_PONS_RPC_URL, callback=rpc, repeat=True)
        async with aiohttp.ClientSession() as session:
            assert await rh_pons.poll_once(session, db, settings) == 0
    assert (
        await db.get_curve_scan_checkpoint(DEP.chain_id, DEP.version, DEP.factory)
        is None
    )
    assert "rh_pons" not in await db.load_ingest_watchdog_state()
    await db.close()


async def test_poll_detects_reorg_without_removed_provider_logs(
    tmp_path, settings_factory, monkeypatch
):
    from aioresponses import CallbackResult

    db = await _db(tmp_path)
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (_verified_dep(),))
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_RPC_URL="https://rpc.invalid",
        RH_PONS_POLL_EVERY_N_CYCLES=1,
        RH_PONS_REORG_OVERLAP_BLOCKS=3,
    )
    reorg = False

    def rpc(url, **kw):
        body = kw["json"]
        method = body["method"]
        if method == "eth_chainId":
            result = hex(DEP.chain_id)
        elif method == "eth_blockNumber":
            result = hex(101)
        elif method == "eth_getBlockByNumber":
            height = int(body["params"][0], 16)
            result = {
                "number": hex(height),
                "hash": "0x" + ("cc" if reorg and height == 101 else "bb") * 32,
                "timestamp": hex(1700000000),
            }
        elif isinstance(body["params"][0]["address"], str):
            result = [_launch_log()] + (
                [] if reorg else [_graduated_log(block=101, log_index=1)]
            )
        else:
            result = []
        return CallbackResult(payload={"result": result})

    with aioresponses() as m:
        m.post(settings.RH_PONS_RPC_URL, callback=rpc, repeat=True)
        async with aiohttp.ClientSession() as session:
            assert await rh_pons.poll_once(session, db, settings) == 2
            assert (await db.get_curve_launch(DEP.chain_id, TOKEN))[
                "lifecycle_status"
            ] == "on_v4"
            reorg = True
            await rh_pons.poll_once(session, db, settings)
    assert (await db.get_curve_launch(DEP.chain_id, TOKEN))[
        "lifecycle_status"
    ] == "on_curve"
    assert (
        await db.canonical_curve_event_block_hash(DEP.chain_id, "0x" + "aa" * 32, 1)
        is None
    )
    assert (await db.get_curve_scan_checkpoint(DEP.chain_id, DEP.version, DEP.factory))[
        "next_block"
    ] == 102
    await db.close()


async def test_trade_emitted_before_factory_launch_resolves_token(
    tmp_path, settings_factory
):
    db = await _db(tmp_path)
    await _collect(
        [_buy_log(log_index=0), _launch_log(log_index=1)], db, settings_factory()
    )
    cur = await db._conn.execute(
        "SELECT token_address FROM curve_launch_events WHERE event_name='curve_buy'"
    )
    assert (await cur.fetchone())[0] == TOKEN
    await db.close()


@pytest.mark.parametrize("archive_start,expected_start", [(None, 991), (90, 90)])
async def test_cold_start_coverage_is_explicit(
    tmp_path, settings_factory, monkeypatch, archive_start, expected_start
):
    from aioresponses import CallbackResult

    db = await _db(tmp_path)
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (_verified_dep(),))
    overrides = {} if archive_start is None else {"RH_PONS_START_BLOCK": archive_start}
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_RPC_URL="https://rpc.invalid",
        RH_PONS_POLL_EVERY_N_CYCLES=1,
        RH_PONS_BACKFILL_BLOCK_SPAN=10,
        **overrides,
    )
    scans = []

    def rpc(url, **kw):
        body = kw["json"]
        method = body["method"]
        if method == "eth_chainId":
            result = hex(DEP.chain_id)
        elif method == "eth_blockNumber":
            result = hex(1000)
        elif method == "eth_getBlockByNumber":
            result = {
                "number": body["params"][0],
                "hash": "0x" + "bb" * 32,
                "timestamp": hex(1700000000),
            }
        else:
            scans.append(body["params"][0])
            result = []
        return CallbackResult(payload={"result": result})

    with aioresponses() as m:
        m.post(settings.RH_PONS_RPC_URL, callback=rpc, repeat=True)
        async with aiohttp.ClientSession() as session:
            await rh_pons.poll_once(session, db, settings)
    assert int(scans[0]["fromBlock"], 16) == expected_start
    assert (await db.get_curve_scan_checkpoint(DEP.chain_id, DEP.version, DEP.factory))[
        "next_block"
    ] == expected_start + 10
    await db.close()


async def test_curve_rpc_filters_initialization_topics(
    tmp_path, settings_factory, monkeypatch
):
    from aioresponses import CallbackResult

    db = await _db(tmp_path)
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (_verified_dep(),))
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_RPC_URL="https://rpc.invalid",
        RH_PONS_POLL_EVERY_N_CYCLES=1,
    )
    init_log = _log(address=CURVE, topics=["0x" + "ff" * 32], log_index=2)

    def rpc(url, **kw):
        body = kw["json"]
        method = body["method"]
        if method == "eth_chainId":
            result = hex(DEP.chain_id)
        elif method == "eth_blockNumber":
            result = hex(100)
        elif method == "eth_getBlockByNumber":
            result = {
                "number": body["params"][0],
                "hash": "0x" + "bb" * 32,
                "timestamp": hex(1700000000),
            }
        else:
            query = body["params"][0]
            if isinstance(query["address"], str):
                result = [_launch_log()]
            else:
                allowed = query.get("topics", [None])[0]
                result = [
                    log
                    for log in [_buy_log(log_index=1), init_log]
                    if allowed is None or log["topics"][0] in allowed
                ]
        return CallbackResult(payload={"result": result})

    with aioresponses() as m:
        m.post(settings.RH_PONS_RPC_URL, callback=rpc, repeat=True)
        async with aiohttp.ClientSession() as session:
            assert await rh_pons.poll_once(session, db, settings) == 2
    assert (await db.get_curve_scan_checkpoint(DEP.chain_id, DEP.version, DEP.factory))[
        "next_block"
    ] == 101
    await db.close()


async def test_resume_never_jumps_to_new_initial_start(
    tmp_path, settings_factory, monkeypatch
):
    from aioresponses import CallbackResult

    db = await _db(tmp_path)
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (_verified_dep(),))
    await db.save_curve_scan_checkpoint(
        DEP.chain_id, DEP.version, DEP.factory, next_block=120, block_hashes={}
    )
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_RPC_URL="https://rpc.invalid",
        RH_PONS_POLL_EVERY_N_CYCLES=1,
        RH_PONS_START_BLOCK=999,
        RH_PONS_BACKFILL_BLOCK_SPAN=10,
    )
    scans = []
    events = []
    monkeypatch.setattr(
        rh_pons.logger, "info", lambda event, **fields: events.append((event, fields))
    )

    def rpc(url, **kw):
        body = kw["json"]
        method = body["method"]
        if method == "eth_chainId":
            result = hex(DEP.chain_id)
        elif method == "eth_blockNumber":
            result = hex(1000)
        elif method == "eth_getBlockByNumber":
            result = {
                "number": body["params"][0],
                "hash": "0x" + "bb" * 32,
                "timestamp": hex(1700000000),
            }
        else:
            scans.append(body["params"][0])
            result = []
        return CallbackResult(payload={"result": result})

    with aioresponses() as m:
        m.post(settings.RH_PONS_RPC_URL, callback=rpc, repeat=True)
        async with aiohttp.ClientSession() as session:
            await rh_pons.poll_once(session, db, settings)
    assert int(scans[0]["toBlock"], 16) == 129
    completed = [fields for event, fields in events if event == "rh_pons_scan_complete"]
    assert completed[0]["lag_blocks"] == 871
    assert (await db.load_ingest_watchdog_state())["rh_pons"] == 0
    await db.close()


async def test_reorg_rechecks_recently_graduated_curve(
    tmp_path, settings_factory, monkeypatch
):
    from aioresponses import CallbackResult

    db = await _db(tmp_path)
    monkeypatch.setattr(rh_pons, "PONS_DEPLOYMENTS", (_verified_dep(),))
    await _collect(
        [_launch_log(), _graduated_log(block=199, log_index=1)], db, settings_factory()
    )
    await db.save_curve_scan_checkpoint(
        DEP.chain_id,
        DEP.version,
        DEP.factory,
        next_block=201,
        block_hashes={
            "197": "0x" + "bb" * 32,
            "198": "0x" + "bb" * 32,
            "199": "0x" + "bb" * 32,
            "200": "0x" + "bb" * 32,
        },
    )
    settings = settings_factory(
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_RPC_URL="https://rpc.invalid",
        RH_PONS_POLL_EVERY_N_CYCLES=1,
        RH_PONS_REORG_OVERLAP_BLOCKS=3,
    )

    def rpc(url, **kw):
        body = kw["json"]
        method = body["method"]
        if method == "eth_chainId":
            result = hex(DEP.chain_id)
        elif method == "eth_blockNumber":
            result = hex(201)
        elif method == "eth_getBlockByNumber":
            height = int(body["params"][0], 16)
            result = {
                "number": hex(height),
                "hash": "0x" + ("cc" if height >= 199 else "bb") * 32,
                "timestamp": hex(1700000000),
            }
        elif isinstance(body["params"][0]["address"], str):
            result = []
        else:
            result = [_buy_log(block=200, block_hash="0x" + "cc" * 32, log_index=2)]
        return CallbackResult(payload={"result": result})

    with aioresponses() as m:
        m.post(settings.RH_PONS_RPC_URL, callback=rpc, repeat=True)
        async with aiohttp.ClientSession() as session:
            assert await rh_pons.poll_once(session, db, settings) == 1
    assert (await db.get_curve_launch(DEP.chain_id, TOKEN))[
        "lifecycle_status"
    ] == "on_curve"
    await db.close()
