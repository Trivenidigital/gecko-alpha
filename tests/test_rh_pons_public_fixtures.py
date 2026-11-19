"""Offline characterization using actual public chain reads, not synthetic logs.

Explorer null topic padding is normalized explicitly; canonical hashes and
clocks come from separately captured public RPC headers. No network in tests.
"""

import copy
import json
from pathlib import Path

from scout.db import Database
from scout.ingestion.rh_pons import PONS_DEPLOYMENTS, collect_from_logs, decode_log

EVIDENCE = (
    Path(__file__).resolve().parents[1]
    / "investigation/rh_pons_trade_fixture_20260913.json"
)
TOKEN = "0xa89c296c92518e8cee489d8e7365f229247ee200"
CURVE = "0x9ba24b3a293f78c39da9a41ef04b69e6ef70f6e1"


def _captured_logs():
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    headers = {
        c["header"]["number"]: c["header"] for c in evidence["calls"] if "header" in c
    }
    logs = {}
    for name, original in evidence["raw_logs"].items():
        entry = copy.deepcopy(original)
        # Legacy explorer responses pad unused indexed topic slots with null.
        entry["topics"] = [topic for topic in entry["topics"] if topic is not None]
        header = headers[entry["blockNumber"]]
        assert header["timestamp"] == original["timeStamp"]
        entry["blockHash"] = header["hash"]
        entry["blockTimestamp"] = header["timestamp"]
        logs[name] = entry
    return logs


def test_decode_actual_public_launch_buy_sell():
    logs = _captured_logs()
    launch = decode_log(logs["launch"])
    assert launch["token_address"] == TOKEN
    assert launch["curve_address"] == CURVE
    assert launch["fields"]["graduation_threshold"] == "4200000000000000000"
    buy = decode_log(logs["buy"])
    assert buy["event_name"] == "curve_buy"
    assert buy["curve_address"] == CURVE
    assert buy["fields"] == {
        "buyer": "0x0a94698cc0cc61115926072d5cb0da03c291fffd",
        "recipient": "0x97eec358d08dadfbf470c4c35e863c91db893ada",
        "amount_in": "168000000000000000",
        "amount_out": "84936220054537801284749547",
        "fee": "12062400000000000",
        "tax": "0",
    }
    sell = decode_log(logs["sell"])
    assert sell["event_name"] == "curve_sell"
    assert sell["curve_address"] == CURVE
    assert sell["fields"]["amount_in"] == "3694180433578018361541818"
    assert sell["fields"]["amount_out"] == "14119086601530037"
    assert sell["fields"]["fee"] == "142617036379091"
    assert sell["fields"]["tax"] == "0"


async def test_collect_actual_public_replay_preserves_identity_and_ineligibility(
    tmp_path, settings_factory
):
    logs = _captured_logs()
    db = Database(tmp_path / "public-replay.db")
    await db.initialize()
    try:
        result = await collect_from_logs(
            [logs["sell"], logs["buy"], logs["launch"]],
            db,
            settings_factory(),
            source="blockscout_public_with_robinhood_rpc_headers",
            provenance="onchain_read_replay",
            deployment=PONS_DEPLOYMENTS[0],
        )
        assert result["recorded_events"] == 3
        assert result["new_launches"] == 1
        assert result["undecodable"] == 0
        discovery = await db.get_curve_launch(4663, TOKEN)
        assert discovery["curve_address"] == CURVE
        assert not discovery["execution_eligible"]
        cursor = await db._conn.execute(
            "SELECT event_name, token_address, block_hash, event_time, provenance "
            "FROM curve_launch_events ORDER BY block_number, log_index"
        )
        rows = await cursor.fetchall()
        assert [row[0] for row in rows] == ["token_launched", "curve_buy", "curve_sell"]
        assert all(row[1] == TOKEN and row[4] == "onchain_read_replay" for row in rows)
        for row, name in zip(rows, ["launch", "buy", "sell"]):
            assert row[2] == logs[name]["blockHash"]
            assert row[3].startswith("2026-09-13T18:34:")
    finally:
        await db.close()
