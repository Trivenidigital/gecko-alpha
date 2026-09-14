"""Read-only public RPC transport comparison; no configuration or DB access."""
import asyncio
import json
import time

import aiohttp
from eth_utils import keccak

URL = "https://rpc.mainnet.chain.robinhood.com"


async def main():
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        async def post(payload):
            async with session.post(URL, json=payload) as response:
                return response.status, await response.json()

        _, head_response = await post({"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []})
        head = int(head_response["result"], 16)
        output = {"scope": "read_only_public_header_transport", "head": head, "rounds": []}
        for size in (50, 100):
            payload = [{"jsonrpc": "2.0", "id": i, "method": "eth_getBlockByNumber", "params": [hex(head-i), False]} for i in range(size)]
            started = time.monotonic()
            status, result = await post(payload)
            valid = isinstance(result, list) and len(result) == size and {r.get("id") for r in result} == set(range(size)) and all(isinstance(r.get("result"), dict) and int(r["result"]["number"], 16) == head-r["id"] for r in result)
            output["rounds"].append({"transport": "batch", "size": size, "status": status, "valid": valid, "seconds": round(time.monotonic()-started, 3)})
        started = time.monotonic()
        result = await asyncio.gather(*(post(p) for p in payload[:8]))
        output["rounds"].append({"transport": "parallel", "size": 8, "valid": all(s == 200 and isinstance(r.get("result"), dict) for s, r in result), "seconds": round(time.monotonic()-started, 3)})
        topics = ["0x"+keccak(text="CurveBuy(address,address,uint256,uint256,uint256,uint256)").hex(), "0x"+keccak(text="CurveSell(address,address,uint256,uint256,uint256,uint256)").hex()]
        for span in (200, 1000, 2000):
            started = time.monotonic()
            status, result = await post({"jsonrpc": "2.0", "id": 50, "method": "eth_getLogs", "params": [{"fromBlock": hex(head-span+1), "toBlock": hex(head), "topics": [topics]}]})
            logs = result.get("result")
            output["rounds"].append({"transport": "topic_only_logs", "span": span, "status": status, "valid": isinstance(logs, list), "logs": len(logs) if isinstance(logs, list) else None, "emitters": len({r["address"] for r in logs}) if isinstance(logs, list) else None, "block_timestamp_present": all("blockTimestamp" in r for r in logs) if logs else None, "seconds": round(time.monotonic()-started, 3)})
        print(json.dumps(output))


if __name__ == "__main__":
    asyncio.run(main())
