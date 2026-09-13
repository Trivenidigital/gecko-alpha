"""Bounded public-RPC acceptance probe; never reads production configuration."""
import asyncio
import json
import tempfile
import time
from pathlib import Path

import aiohttp

from scout.config import Settings
from scout.db import Database
from scout.ingestion.rh_pons import active_deployment, poll_once


async def main():
    settings = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="unused",
        TELEGRAM_CHAT_ID="unused",
        ANTHROPIC_API_KEY="unused",
        RH_PONS_COLLECTOR_ENABLED=True,
        RH_PONS_RPC_URL="https://rpc.mainnet.chain.robinhood.com",
        RH_PONS_POLL_EVERY_N_CYCLES=1,
        RH_PONS_BACKFILL_BLOCK_SPAN=200,
        RH_PONS_INITIAL_LOOKBACK_BLOCKS=200,
        RH_PONS_REORG_OVERLAP_BLOCKS=3,
    )
    dep = active_deployment()
    with tempfile.TemporaryDirectory(prefix="rh-pons-evidence-") as folder:
        db = Database(Path(folder) / "isolated.db")
        await db.initialize()
        try:
            async with aiohttp.ClientSession(trust_env=True) as session:
                scans = []
                for _ in range(2):
                    started = time.monotonic()
                    events = await poll_once(session, db, settings)
                    cp = await db.get_curve_scan_checkpoint(dep.chain_id, dep.version, dep.factory)
                    scans.append({"events": events, "duration_seconds": round(time.monotonic()-started, 3), "checkpoint": cp})
                cur = await db._conn.execute("SELECT updated_at FROM ingest_watchdog_state WHERE source='rh_pons'")
                before = await cur.fetchone()
                assert before and all(s["checkpoint"] for s in scans), "Live scan did not complete"
                settings.RH_PONS_RPC_URL = "http://127.0.0.1:1"
                await poll_once(session, db, settings)
                cur = await db._conn.execute("SELECT updated_at FROM ingest_watchdog_state WHERE source='rh_pons'")
                after = await cur.fetchone()
                assert before == after, "Failure refreshed success heartbeat"
                cur = await db._conn.execute("SELECT COUNT(*), SUM(execution_eligible) FROM curve_launch_discoveries")
                count, eligible = await cur.fetchone()
                assert not eligible
                print(json.dumps({"scope": "isolated_public_rpc_acceptance", "production_mutated": False, "scans": scans, "launches": count, "execution_eligible": eligible or 0, "failure_preserved_heartbeat": True}))
        finally:
            await db.close()


if __name__ == "__main__":
    asyncio.run(main())
