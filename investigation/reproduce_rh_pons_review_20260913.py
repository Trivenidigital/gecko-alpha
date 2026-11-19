"""Read-only-to-production reproductions against reviewed b8843487.

These assertions document DEFECTS, not desired contracts. Uses temporary DBs,
synthetic fixtures and a mocked RPC. Run from repository root with
python investigation/reproduce_rh_pons_review_20260913.py.
"""

import asyncio
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scout.ingestion import rh_pons


async def cursor_reproduction():
    dep = rh_pons.PonsDeployment(
        version="pons_v2",
        chain_id=4663,
        network="robinhood",
        factory=rh_pons.PONS_DEPLOYMENTS[0].factory,
        verification_status="onchain_verified",
        sources=("synthetic",),
        deploy_block=90,
    )
    settings = SimpleNamespace(
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_POLL_EVERY_N_CYCLES=1,
        RH_PONS_RPC_URL="https://rpc.example.invalid",
        RH_PONS_BACKFILL_BLOCK_SPAN=1000,
    )
    db = SimpleNamespace(max_curve_event_block=AsyncMock(return_value=None))
    rpc = AsyncMock(return_value=[])
    with (
        patch.object(rh_pons, "active_deployment", return_value=dep),
        patch.object(rh_pons, "_rpc_get_logs", rpc),
    ):
        await rh_pons.poll_once(None, db, settings)
        await rh_pons.poll_once(None, db, settings)
    ranges = [
        [c.kwargs["from_block"], c.kwargs["to_block"]] for c in rpc.call_args_list
    ]
    assert ranges == [[90, 1089], [90, 1089]], ranges
    assert all(c.kwargs["address"] == dep.factory for c in rpc.call_args_list)
    return {"empty_range_requests": ranges, "only_address_queried": dep.factory}


def latency_reproduction():
    spec = importlib.util.spec_from_file_location(
        "latency_review", ROOT / "scripts/compare_discovery_latency.py"
    )
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    with sqlite3.connect(":memory:") as conn:
        conn.executescript("""
        CREATE TABLE curve_launch_discoveries (
          chain_id INTEGER, network TEXT, protocol TEXT, token_address TEXT,
          lifecycle_status TEXT, event_time TEXT, first_seen_at TEXT, provenance TEXT);
        CREATE TABLE candidates (contract_address TEXT, chain TEXT, first_seen_at TEXT);
        CREATE TABLE dex_pool_discoveries
          (base_token_address TEXT, network TEXT, first_seen_at TEXT);
        INSERT INTO curve_launch_discoveries VALUES
          (4663, 'robinhood', 'pons_v2', '0x1111', 'on_curve',
           '2026-09-13T10:00:00Z', '2026-09-13T10:00:30Z', 'synthetic');
        INSERT INTO candidates VALUES ('0x1111', 'ethereum', '2026-09-12T10:00:00Z');
        INSERT INTO dex_pool_discoveries VALUES ('0x1111', 'eth', '2026-09-12T10:00:00Z');
        """)
        row = harness.compare(conn)["launches"][0]
    assert row["event_to_cg_ds_gt_seconds"] == -86400
    assert row["event_to_dex_lane_seconds"] == -86400
    assert row["censored"] == []
    return {
        "foreign_chain_cg_latency": row["event_to_cg_ds_gt_seconds"],
        "foreign_chain_dex_latency": row["event_to_dex_lane_seconds"],
        "censored": row["censored"],
    }


if __name__ == "__main__":
    print(
        json.dumps(
            {
                "cursor": asyncio.run(cursor_reproduction()),
                "cross_chain_latency": latency_reproduction(),
            },
            indent=2,
        )
    )
