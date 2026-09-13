"""RH Pons RPC capability preflight — mocked HTTP only, no live requests."""

from __future__ import annotations

import json

import aiohttp
import pytest
from aioresponses import CallbackResult, aioresponses

from scripts.check_rh_pons_rpc import main, run_checks
from scout.ingestion.rh_pons import PONS_DEPLOYMENTS

SECRET_URL = "https://rpc.example.test/v2/sekrit-api-key-123"
FACTORY = next(d.factory for d in PONS_DEPLOYMENTS if d.version == "pons_v2")
GOOD_BLOCK = {
    "number": "0x64",
    "hash": "0x" + "ab" * 32,
    "parentHash": "0x" + "cd" * 32,
    "timestamp": "0x68c4a000",
}


def _rpc(handlers: dict, calls: list):
    """aioresponses callback dispatching on the JSON-RPC method."""

    def cb(url, **kwargs):
        body = kwargs["json"]
        calls.append(body)
        h = handlers[body["method"]]
        if isinstance(h, int):
            return CallbackResult(status=h, body="denied")
        if isinstance(h, tuple) and h[0] == "error":
            payload = {"jsonrpc": "2.0", "id": body["id"], "error": h[1]}
        else:
            payload = {"jsonrpc": "2.0", "id": body["id"], "result": h}
        return CallbackResult(status=200, payload=payload)

    return cb


def _handlers(**over):
    base = {
        "eth_chainId": hex(4663),
        "eth_blockNumber": "0x64",
        "eth_getCode": "0x6080604052",
        "eth_getBlockByNumber": GOOD_BLOCK,
        "eth_getLogs": [],
    }
    base.update(over)
    return base


async def _run(handlers, calls):
    with aioresponses() as m:
        m.post(SECRET_URL, callback=_rpc(handlers, calls), repeat=True)
        async with aiohttp.ClientSession() as session:
            return await run_checks(session, SECRET_URL, log_window=10)


def _by_name(report):
    return {c["name"]: c for c in report["checks"]}


async def test_success_ready_with_capability_only_label():
    calls: list = []
    report = await _run(_handlers(), calls)
    assert report["overall"] == "ready"
    assert report["scope"] == "rpc_capability_only"
    assert "observation activation" in report["disclaimer"]
    assert "NOT" in report["disclaimer"]
    checks = _by_name(report)
    assert set(checks) == {
        "eth_chainId",
        "eth_blockNumber",
        "eth_getCode",
        "eth_getBlockByNumber",
        "eth_getLogs",
    }
    assert all(c["status"] == "ok" for c in checks.values())
    assert checks["eth_getLogs"]["log_count"] == 0
    # bounded window over the factory only
    logs_call = next(c for c in calls if c["method"] == "eth_getLogs")
    flt = logs_call["params"][0]
    assert flt["address"] == FACTORY
    assert int(flt["toBlock"], 16) == 100
    assert int(flt["toBlock"], 16) - int(flt["fromBlock"], 16) == 9
    code_call = next(c for c in calls if c["method"] == "eth_getCode")
    assert code_call["params"][0] == FACTORY
    # read-only: no transaction methods ever sent
    assert all(not c["method"].startswith(("eth_send", "eth_sign")) for c in calls)
    assert SECRET_URL not in json.dumps(report)
    assert "sekrit" not in json.dumps(report)


async def test_wrong_chain_is_error_and_later_checks_skipped():
    calls: list = []
    report = await _run(_handlers(eth_chainId="0x1"), calls)
    assert report["overall"] == "error"
    checks = _by_name(report)
    assert checks["eth_chainId"]["status"] == "error"
    assert checks["eth_chainId"]["error_kind"] == "chain_mismatch"
    assert checks["eth_chainId"]["observed"] == 1
    assert checks["eth_getLogs"]["status"] == "skipped"
    assert [c["method"] for c in calls] == ["eth_chainId"]


async def test_http_403_on_logs_is_http_error():
    report = await _run(_handlers(eth_getLogs=403), [])
    assert report["overall"] == "error"
    logs = _by_name(report)["eth_getLogs"]
    assert logs["status"] == "error"
    assert logs["error_kind"] == "http_error"
    assert logs["http_status"] == 403
    assert _by_name(report)["eth_getCode"]["status"] == "ok"
    assert "sekrit" not in json.dumps(report)


async def test_jsonrpc_error_is_distinguished():
    report = await _run(
        _handlers(
            eth_getLogs=("error", {"code": -32005, "message": "range too large"})
        ),
        [],
    )
    logs = _by_name(report)["eth_getLogs"]
    assert logs["error_kind"] == "jsonrpc_error"
    assert logs["rpc_error_code"] == -32005
    assert report["overall"] == "error"


@pytest.mark.parametrize(
    "over,name",
    [
        ({"eth_blockNumber": "banana"}, "eth_blockNumber"),
        ({"eth_getCode": "0x"}, "eth_getCode"),
        ({"eth_getBlockByNumber": {"number": "0x64"}}, "eth_getBlockByNumber"),
        ({"eth_getLogs": {"not": "a list"}}, "eth_getLogs"),
    ],
)
async def test_malformed_results_are_shape_errors(over, name):
    report = await _run(_handlers(**over), [])
    assert report["overall"] == "error"
    assert _by_name(report)[name]["error_kind"] == "shape_error"


class _S:
    def __init__(self, url):
        self.RH_PONS_RPC_URL = url


async def test_no_configured_url_exits_nonzero(capsys):
    rc = await main([], settings_factory=lambda: _S(""))
    out = json.loads(capsys.readouterr().out)
    assert rc == 1
    assert out["overall"] == "error"
    assert out["checks"][0]["error_kind"] == "not_configured"


async def test_cli_uses_configured_url_and_redacts(capsys):
    with aioresponses() as m:
        m.post(SECRET_URL, callback=_rpc(_handlers(), []), repeat=True)
        rc = await main([], settings_factory=lambda: _S(SECRET_URL))
    raw = capsys.readouterr().out
    assert rc == 0
    assert json.loads(raw)["endpoint"] == "https://rpc.example.test"
    assert "sekrit" not in raw


async def test_cli_rpc_url_override_and_failure_exit(capsys):
    public = "https://public.example.test/rpc"
    with aioresponses() as m:
        m.post(public, callback=_rpc(_handlers(eth_chainId="0x1"), []), repeat=True)
        rc = await main(["--rpc-url", public], settings_factory=lambda: _S(SECRET_URL))
    assert rc == 1
    assert json.loads(capsys.readouterr().out)["endpoint"] == (
        "https://public.example.test"
    )
