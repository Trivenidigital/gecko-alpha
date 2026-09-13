#!/usr/bin/env python3
"""Read-only RPC capability preflight for the Robinhood-chain Pons collector.

WHAT THIS PROVES — and what it does not. A green report means the endpoint
answers the five read methods the collector depends on, on the declared chain
(4663), and that the recorded pons_v2 factory address has bytecode there. It
does NOT verify the event ABI, the deployment registry's verification label,
or that early discovery works: an empty, well-formed eth_getLogs response
proves the endpoint accepts the query, nothing about what it would decode.
Observation activation is a separate, operator-owned step.

SAFETY POSTURE.
  - Read methods only; no transaction or signing method is ever sent.
  - Writes nothing: no flags, registry, config, or DB.
  - The endpoint URL may carry an API key in its path or query, so reports
    and errors carry only scheme://host, and never an exception's text
    (aiohttp embeds the full URL in its messages).

Exit codes:
  0 — every check ok (rpc capability only)
  1 — any check failed, or no RPC URL configured
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scout.ingestion.rh_pons import PONS_DEPLOYMENTS, ROBINHOOD_CHAIN_ID  # noqa: E402

SCOPE = "rpc_capability_only"
DISCLAIMER = (
    "RPC capability only. An empty valid eth_getLogs result does NOT verify the "
    "Pons ABI, the deployment registry, or early discovery. Observation "
    "activation is a separate step."
)
CHECK_NAMES = (
    "eth_chainId",
    "eth_blockNumber",
    "eth_getCode",
    "eth_getBlockByNumber",
    "eth_getLogs",
)
DEFAULT_LOG_WINDOW = 10
REQUEST_TIMEOUT_SEC = 15


class CheckFailed(Exception):
    """A check failure carrying only redaction-safe fields."""

    def __init__(self, kind: str, **fields: Any) -> None:
        super().__init__(kind)
        self.kind = kind
        self.fields = fields


def redact_endpoint(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or "unknown"
    return f"{parts.scheme or 'unknown'}://{host}"


def _is_hex_quantity(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)", value) is not None
    )


def _is_hex_data(value: Any) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value) is not None
    )


def _is_hash(value: Any) -> bool:
    return _is_hex_data(value) and len(value) == 66


async def _call(
    session: aiohttp.ClientSession, url: str, method: str, params: list, req_id: int
) -> Any:
    payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    try:
        async with session.post(
            url,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC),
        ) as resp:
            if resp.status != 200:
                raise CheckFailed("http_error", http_status=resp.status)
            try:
                body = await resp.json(content_type=None)
            except (ValueError, aiohttp.ContentTypeError):
                raise CheckFailed("shape_error", detail="response is not JSON")
    except CheckFailed:
        raise
    except asyncio.TimeoutError:
        raise CheckFailed("transport_error", detail="timeout")
    except aiohttp.ClientError as exc:
        raise CheckFailed("transport_error", detail=type(exc).__name__)
    if not isinstance(body, dict):
        raise CheckFailed("shape_error", detail="response is not a JSON object")
    if body.get("error") is not None:
        err = body["error"]
        code = err.get("code") if isinstance(err, dict) else None
        raise CheckFailed(
            "jsonrpc_error", rpc_error_code=code if isinstance(code, int) else None
        )
    if "result" not in body:
        raise CheckFailed("shape_error", detail="missing result")
    return body["result"]


async def run_checks(
    session: aiohttp.ClientSession,
    rpc_url: str,
    *,
    log_window: int = DEFAULT_LOG_WINDOW,
) -> dict:
    factory = next(d.factory for d in PONS_DEPLOYMENTS if d.version == "pons_v2")
    results: dict[str, dict] = {}
    state: dict[str, Any] = {}

    async def chain_id() -> dict:
        r = await _call(session, rpc_url, "eth_chainId", [], 1)
        if not _is_hex_quantity(r):
            raise CheckFailed("shape_error", detail="chainId not a hex quantity")
        observed = int(r, 16)
        if observed != ROBINHOOD_CHAIN_ID:
            raise CheckFailed(
                "chain_mismatch", observed=observed, expected=ROBINHOOD_CHAIN_ID
            )
        return {"observed": observed}

    async def block_number() -> dict:
        r = await _call(session, rpc_url, "eth_blockNumber", [], 2)
        if not _is_hex_quantity(r):
            raise CheckFailed("shape_error", detail="blockNumber not a hex quantity")
        state["latest"] = int(r, 16)
        return {"latest_block": state["latest"]}

    async def get_code() -> dict:
        r = await _call(session, rpc_url, "eth_getCode", [factory, "latest"], 3)
        if not _is_hex_data(r):
            raise CheckFailed("shape_error", detail="code not a hex string")
        if len(r) <= 2:
            raise CheckFailed("shape_error", detail="empty bytecode at factory")
        return {"factory": factory, "code_bytes": (len(r) - 2) // 2}

    async def get_block() -> dict:
        tag = hex(state["latest"]) if "latest" in state else "latest"
        r = await _call(session, rpc_url, "eth_getBlockByNumber", [tag, False], 4)
        if not (
            isinstance(r, dict)
            and _is_hex_quantity(r.get("number"))
            and _is_hash(r.get("hash"))
            and _is_hash(r.get("parentHash"))
            and _is_hex_quantity(r.get("timestamp"))
            and ("latest" not in state or int(r["number"], 16) == state["latest"])
        ):
            raise CheckFailed("shape_error", detail="invalid block header")
        return {"block": int(r["number"], 16), "timestamp": int(r["timestamp"], 16)}

    async def get_logs() -> dict:
        if "latest" not in state:
            raise CheckFailed("skipped_dependency", detail="no latest block")
        to_block = state["latest"]
        from_block = max(0, to_block - max(1, log_window) + 1)
        flt = {
            "address": factory,
            "fromBlock": hex(from_block),
            "toBlock": hex(to_block),
        }
        r = await _call(session, rpc_url, "eth_getLogs", [flt], 5)
        if not isinstance(r, list):
            raise CheckFailed("shape_error", detail="logs not a list")
        for log in r:
            if not (
                isinstance(log, dict)
                and str(log.get("address", "")).lower() == factory.lower()
                and _is_hex_quantity(log.get("blockNumber"))
                and from_block <= int(log["blockNumber"], 16) <= to_block
                and _is_hash(log.get("blockHash"))
                and _is_hash(log.get("transactionHash"))
                and _is_hex_quantity(log.get("logIndex"))
                and isinstance(log.get("topics"), list)
                and all(_is_hash(topic) for topic in log["topics"])
                and _is_hex_data(log.get("data"))
                and log.get("removed", False) is False
            ):
                raise CheckFailed("shape_error", detail="invalid or out-of-range log")
        return {"from_block": from_block, "to_block": to_block, "log_count": len(r)}

    steps: list[tuple[str, Callable]] = [
        ("eth_chainId", chain_id),
        ("eth_blockNumber", block_number),
        ("eth_getCode", get_code),
        ("eth_getBlockByNumber", get_block),
        ("eth_getLogs", get_logs),
    ]
    chain_ok = False
    for name, fn in steps:
        if name != "eth_chainId" and not chain_ok:
            results[name] = {"name": name, "status": "skipped"}
            continue
        try:
            results[name] = {"name": name, "status": "ok", **(await fn())}
            if name == "eth_chainId":
                chain_ok = True
        except CheckFailed as exc:
            results[name] = {
                "name": name,
                "status": "error",
                "error_kind": exc.kind,
                **exc.fields,
            }

    checks = [results[n] for n in CHECK_NAMES]
    ready = all(c["status"] == "ok" for c in checks)
    return _report(rpc_url, checks, ready)


def _report(rpc_url: str | None, checks: list[dict], ready: bool) -> dict:
    return {
        "scope": SCOPE,
        "disclaimer": DISCLAIMER,
        "endpoint": redact_endpoint(rpc_url) if rpc_url else None,
        "declared_chain_id": ROBINHOOD_CHAIN_ID,
        "overall": "ready" if ready else "error",
        "checks": checks,
    }


def _default_settings():
    from scout.config import Settings

    return Settings()


async def main(
    argv: list[str] | None = None,
    *,
    settings_factory: Callable[[], Any] = _default_settings,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--rpc-url",
        help="Override Settings.RH_PONS_RPC_URL (e.g. a public endpoint).",
    )
    parser.add_argument("--log-window", type=int, default=DEFAULT_LOG_WINDOW)
    args = parser.parse_args(argv)

    rpc_url = args.rpc_url or (settings_factory().RH_PONS_RPC_URL or "")
    if not rpc_url:
        report = _report(
            None,
            [{"name": "config", "status": "error", "error_kind": "not_configured"}],
            False,
        )
    else:
        # Honor the host's standard proxy configuration for the operator CLI.
        async with aiohttp.ClientSession(trust_env=True) as session:
            report = await run_checks(
                session, rpc_url, log_window=min(max(1, args.log_window), 100)
            )
    print(json.dumps(report, indent=2))
    return 0 if report["overall"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
