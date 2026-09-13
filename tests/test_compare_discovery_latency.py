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


def _report(db_path, statements=()):
    with sqlite3.connect(db_path) as conn:
        for sql, params in statements:
            conn.execute(sql, params)
        return harness.compare(conn)


async def test_foreign_chain_address_is_not_an_observation(tmp_path, token_factory):
    path = await _seed(tmp_path, token_factory)
    report = _report(
        path,
        [
            ("UPDATE candidates SET chain='base'", ()),
            ("UPDATE dex_pool_discoveries SET network='base'", ()),
        ],
    )
    row = report["launches"][0]
    assert row["cg_ds_gt_first_seen_at"] is None
    assert row["dex_lane_first_seen_at"] is None


async def test_precision_and_paired_advantage(tmp_path, token_factory):
    path = await _seed(tmp_path, token_factory)
    report = _report(
        path,
        [
            (
                "UPDATE candidates SET first_seen_at=?",
                ("2026-09-13T11:00:30.125+01:00",),
            ),
        ],
    )
    row = report["launches"][0]
    assert row["event_to_cg_ds_gt_seconds"] == 30.125
    assert row["rh_advantage_vs_cg_ds_gt_seconds"] == 0.125
    paired = report["paired_advantage_summary"]["rh_vs_cg_ds_gt"]
    assert paired["n"] == 1
    assert paired["censored_n"] == 1
    assert paired["median_seconds"] == 0.125
    assert report["provenance_counts"] == {"fixture:source_derived": 2}


async def test_invalid_and_negative_clocks_excluded(tmp_path, token_factory):
    path = await _seed(tmp_path, token_factory)
    report = _report(
        path,
        [
            ("UPDATE candidates SET first_seen_at='broken'", ()),
            (
                "UPDATE dex_pool_discoveries SET first_seen_at='2026-09-13T09:59:59Z'",
                (),
            ),
        ],
    )
    row = report["launches"][0]
    assert "invalid_cg_ds_gt_clock" in row["censored"]
    assert "negative_event_to_dex_lane" in row["censored"]
    assert report["latency_summary"]["event_to_dex_lane"]["n"] == 0
    assert report["paired_advantage_summary"]["rh_vs_dex_lane"]["n"] == 0


async def test_alias_identity(tmp_path, token_factory):
    path = await _seed(tmp_path, token_factory)
    report = _report(
        path,
        [
            ("UPDATE curve_launch_discoveries SET chain_id=1, network='eth'", ()),
            ("UPDATE candidates SET chain='ethereum'", ()),
            ("UPDATE dex_pool_discoveries SET network='eth'", ()),
        ],
    )
    assert report["launches"][0]["event_to_cg_ds_gt_seconds"] == 600.0
    assert report["launches"][0]["event_to_dex_lane_seconds"] == 300.0


async def test_cg_slug_not_guessed(tmp_path, token_factory):
    path = await _seed(tmp_path, token_factory)
    report = _report(path, [("UPDATE candidates SET chain='cg:robinhood'", ())])
    assert report["launches"][0]["cg_ds_gt_first_seen_at"] is None


async def test_earliest_absolute_instant_and_negative_advantage(
    tmp_path, token_factory
):
    path = await _seed(tmp_path, token_factory)
    report = _report(
        path,
        [
            (
                "INSERT INTO dex_pool_discoveries (network,pool_address,base_token_address,first_seen_at) VALUES (?,?,?,?)",
                ("robinhood", "another-pool", TOKEN_A, "2026-09-13T11:00:01.5+01:00"),
            ),
        ],
    )
    row = report["launches"][0]
    assert row["event_to_dex_lane_seconds"] == 1.5
    assert row["rh_advantage_vs_dex_lane_seconds"] == -28.5
    assert report["paired_advantage_summary"]["rh_vs_dex_lane"]["rh_later_n"] == 1


async def test_mixed_invalid_and_valid_observations_censor_earliest(
    tmp_path, token_factory
):
    path = await _seed(tmp_path, token_factory)
    report = _report(
        path,
        [
            (
                "INSERT INTO dex_pool_discoveries (network,pool_address,base_token_address,first_seen_at) VALUES (?,?,?,?)",
                ("robinhood", "unknown-clock-pool", TOKEN_A, "broken"),
            ),
        ],
    )
    row = report["launches"][0]
    assert "invalid_dex_lane_clock" in row["censored"]
    assert row["dex_lane_first_seen_at"] is None
    assert row["event_to_dex_lane_seconds"] is None
    assert row["rh_advantage_vs_dex_lane_seconds"] is None
    assert report["latency_summary"]["event_to_dex_lane"]["n"] == 0
    assert report["paired_advantage_summary"]["rh_vs_dex_lane"]["n"] == 0
    assert report["paired_advantage_summary"]["rh_vs_dex_lane"]["censored_n"] == 2
    assert report["latency_summary"]["event_to_cg_ds_gt"]["n"] == 1
