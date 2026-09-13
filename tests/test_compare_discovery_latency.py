"""Latency comparison harness — censoring is explicit, never zero.

Seeds the three lanes' tables through their real write paths, then runs the
read-only harness against the same DB file.
"""

import importlib.util
import sqlite3
from pathlib import Path

from scout.db import Database
from scout.ingestion import rh_pons

_SPEC = importlib.util.spec_from_file_location(
    "compare_discovery_latency",
    Path(__file__).parents[1] / "scripts" / "compare_discovery_latency.py",
)
harness = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(harness)

TOKEN_A = "0x" + "a1" * 20
TOKEN_B = "0x" + "b2" * 20


async def _seed(tmp_path, token_factory):
    db = Database(tmp_path / "t.db")
    await db.initialize()

    # Launch A: event time known; also seen by CG/DS/GT and the DEX lane.
    await db.record_curve_launch_discovery(
        chain_id=rh_pons.ROBINHOOD_CHAIN_ID,
        network="robinhood",
        protocol="pons_v2",
        token_address=TOKEN_A,
        curve_address=None,
        deployer_address=None,
        pair_token_address=None,
        launch_config_id=None,
        graduation_threshold=None,
        lifecycle_status="on_curve",
        event_time="2026-09-13T10:00:00+00:00",
        first_seen_at="2026-09-13T10:00:30+00:00",
        source="fixture:harness",
        provenance="fixture:source_derived",
        execution_eligible=False,
        eligibility_reasons=["deployment_unverified"],
    )
    token = token_factory(contract_address=TOKEN_A, chain="robinhood")
    await db.upsert_candidate(token)
    await db._conn.execute(
        "UPDATE candidates SET first_seen_at = ? WHERE contract_address = ?",
        ("2026-09-13T10:10:00+00:00", TOKEN_A),
    )
    await db._conn.commit()
    await db.record_pool_discovery(
        network="robinhood",
        pool_address="0x" + "c3" * 20,
        base_token_address=TOKEN_A,
        base_token_symbol="AAA",
        quote_token_symbol="ETH",
        pool_created_at="2026-09-13T10:00:00+00:00",
        fdv_usd=None,
        liquidity_usd=2000.0,
        volume_h1_usd=None,
    )
    await db._conn.execute(
        "UPDATE dex_pool_discoveries SET first_seen_at = ? "
        "WHERE base_token_address = ?",
        ("2026-09-13T10:05:00+00:00", TOKEN_A),
    )
    await db._conn.commit()

    # Launch B: no block timestamp, never seen by any other lane — every
    # measurement must come back censored, not zero.
    await db.record_curve_launch_discovery(
        chain_id=rh_pons.ROBINHOOD_CHAIN_ID,
        network="robinhood",
        protocol="pons_v2",
        token_address=TOKEN_B,
        curve_address=None,
        deployer_address=None,
        pair_token_address=None,
        launch_config_id=None,
        graduation_threshold=None,
        lifecycle_status="on_curve",
        event_time=None,
        first_seen_at="2026-09-13T11:00:00+00:00",
        source="fixture:harness",
        provenance="fixture:source_derived",
        execution_eligible=False,
        eligibility_reasons=["deployment_unverified"],
    )
    await db.close()
    return tmp_path / "t.db"


async def test_latencies_and_censoring(tmp_path, token_factory):
    db_path = await _seed(tmp_path, token_factory)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        report = harness.compare(conn)
    finally:
        conn.close()

    assert report["launch_count"] == 2
    by_token = {r["token_address"]: r for r in report["launches"]}

    row_a = by_token[TOKEN_A]
    assert row_a["event_to_rh_seconds"] == 30.0
    assert row_a["event_to_dex_lane_seconds"] == 300.0
    assert row_a["event_to_cg_ds_gt_seconds"] == 600.0
    assert row_a["censored"] == []
    assert row_a["provider_available_at"] == "unknown"

    row_b = by_token[TOKEN_B]
    assert row_b["event_to_rh_seconds"] is None
    assert row_b["censored"] == [
        "event_time_unavailable",
        "never_observed_cg_ds_gt",
        "never_observed_dex_lane",
    ]

    assert report["censoring"] == {
        "event_time_unavailable": 1,
        "never_observed_cg_ds_gt": 1,
        "never_observed_dex_lane": 1,
    }
    assert report["latency_summary"]["event_to_rh"]["n"] == 1
    assert report["latency_summary"]["event_to_rh"]["median_seconds"] == 30.0
