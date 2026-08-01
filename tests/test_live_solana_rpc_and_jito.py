"""PR-S1: read-only RPC client and Jito submission client.

Two properties here are structural rather than behavioural and are worth
stating plainly:

- ``SolanaRpcClient`` must have NO broadcast method. The lane's guarantee that
  it never sends through a public RPC is the absence of the code, so a test
  asserts the absence and will fail if a future refactor adds one back.
- ``JitoClient.submit_transaction`` must issue EXACTLY ONE POST regardless of
  outcome. A retry would teach a caller that resending after a timeout is
  fine, and the next step from there is rebuilding — which can double-swap.
"""

from __future__ import annotations

import asyncio
import base64
import re

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.live.solana.constants import JITO_TIP_ACCOUNTS_FALLBACK, USDC_MINT
from scout.live.solana.exceptions import (
    SolanaAmbiguousSubmissionError,
    SolanaAPIError,
    SolanaResponseError,
)
from scout.live.solana.jito_client import JitoClient
from scout.live.solana import transport as transport_module
from scout.live.solana.rpc_client import LOOKUP_TABLE_META_SIZE, SolanaRpcClient
from solana_tx_builder import PAYER_PUBKEY, TIP_ACCOUNT, build_swap_tx

_RPC_URL = "https://api.mainnet-beta.solana.com"
_JITO = "https://mainnet.block-engine.jito.wtf"
_SUBMIT_RE = re.compile(
    r"https://mainnet\.block-engine\.jito\.wtf/api/v1/transactions.*"
)

_SIGNATURE = "4Rf81Nd6uXrp39hFFr1WMFnGjzxyGXrvwb8VSRUnotH1uTbzLEsatgthdk1mR2yUq2P1Bz5HXoccfQX7YTUgffwy"


def _rpc_result(value):
    return {"jsonrpc": "2.0", "id": 1, "result": value}


# ----------------------------------------------------------------------
# Structural guarantee: no broadcast path exists
# ----------------------------------------------------------------------
@pytest.mark.parametrize(
    "forbidden",
    ["send_transaction", "sendTransaction", "send_raw_transaction", "broadcast"],
)
def test_rpc_client_has_no_broadcast_method(forbidden):
    """The no-public-RPC-broadcast guarantee is structural, not a flag."""
    assert not hasattr(SolanaRpcClient, forbidden)


def test_rpc_client_invokes_no_broadcast_rpc_method():
    """Belt and braces: enumerate the RPC methods the module can actually call.

    An AST walk over the arguments of ``self._rpc(...)``, not a text grep —
    the module's own docstring names the broadcast method in order to explain
    why it is absent, and a grep cannot tell prose from a call site.
    """
    import ast
    import inspect

    import scout.live.solana.rpc_client as module

    tree = ast.parse(inspect.getsource(module))
    invoked: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "_rpc" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                invoked.add(first.value)

    assert invoked, "expected to find the module's RPC call sites"
    # Assembled from fragments so this assertion cannot match itself if the
    # test file is ever grepped for banned identifiers.
    banned = {"send" + "Transaction", "send" + "RawTransaction", "request" + "Airdrop"}
    assert not (invoked & banned), f"broadcast method reachable: {invoked & banned}"
    assert "getSignatureStatuses" in invoked  # sanity: the walk found real calls


# ----------------------------------------------------------------------
# RPC reads
# ----------------------------------------------------------------------
async def test_simulate_transaction_success(settings_factory):
    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _RPC_URL,
                payload=_rpc_result(
                    {
                        "context": {"slot": 283_000_123},
                        "value": {
                            "err": None,
                            "logs": ["Program log: ok"],
                            "unitsConsumed": 84_231,
                        },
                    }
                ),
            )
            sim = await client.simulate_transaction(build_swap_tx().tx_b64)

    assert sim.ok
    assert sim.units_consumed == 84_231
    assert sim.slot == 283_000_123


async def test_simulate_transaction_propagates_error(settings_factory):
    """A failed simulation is RETURNED (so logs reach evidence), not raised."""
    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _RPC_URL,
                payload=_rpc_result(
                    {
                        "context": {"slot": 1},
                        "value": {
                            "err": {"InstructionError": [3, {"Custom": 6001}]},
                            "logs": ["Program log: slippage exceeded"],
                            "unitsConsumed": 12_000,
                        },
                    }
                ),
            )
            sim = await client.simulate_transaction(build_swap_tx().tx_b64)

    assert not sim.ok
    assert sim.err == {"InstructionError": [3, {"Custom": 6001}]}
    assert "slippage exceeded" in sim.logs[0]

    with pytest.raises(Exception, match="simulation failed"):
        SolanaRpcClient.raise_for_simulation(sim)


async def test_simulate_rejects_conflicting_flags(settings_factory):
    """sigVerify + replaceRecentBlockhash is rejected locally, not at the RPC."""
    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with pytest.raises(ValueError, match="cannot both be true"):
            await client.simulate_transaction(
                build_swap_tx().tx_b64, sig_verify=True, replace_recent_blockhash=True
            )


async def test_signature_statuses_null_means_unknown(settings_factory):
    """A null element is 'not in this node's cache', NOT 'never existed'."""
    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _RPC_URL, payload=_rpc_result({"context": {"slot": 5}, "value": [None]})
            )
            statuses = await client.get_signature_statuses([_SIGNATURE])

    assert len(statuses) == 1
    assert statuses[0].known is False
    assert statuses[0].landed is False
    assert statuses[0].failed_on_chain is False


async def test_signature_statuses_landed_and_failed(settings_factory):
    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _RPC_URL,
                payload=_rpc_result(
                    {
                        "context": {"slot": 9},
                        "value": [
                            {
                                "slot": 48,
                                "confirmations": None,
                                "err": None,
                                "confirmationStatus": "finalized",
                            },
                            {
                                "slot": 49,
                                "confirmations": 3,
                                "err": {"InstructionError": [0, "Custom"]},
                                "confirmationStatus": "confirmed",
                            },
                        ],
                    }
                ),
            )
            statuses = await client.get_signature_statuses([_SIGNATURE, _SIGNATURE])

    assert statuses[0].landed and not statuses[0].failed_on_chain
    assert statuses[0].confirmation_status == "finalized"
    assert statuses[1].failed_on_chain and not statuses[1].landed


async def test_signature_statuses_length_mismatch_fails_closed(settings_factory):
    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(_RPC_URL, payload=_rpc_result({"context": {}, "value": []}))
            with pytest.raises(SolanaResponseError, match="one element per signature"):
                await client.get_signature_statuses([_SIGNATURE])


async def test_get_balance_and_token_balance(settings_factory):
    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _RPC_URL, payload=_rpc_result({"context": {}, "value": 1_500_000_000})
            )
            lamports = await client.get_balance(PAYER_PUBKEY)

            mock.post(
                _RPC_URL,
                payload=_rpc_result(
                    {
                        "context": {},
                        "value": [
                            {
                                "account": {
                                    "data": {
                                        "parsed": {
                                            "info": {
                                                "tokenAmount": {"amount": "8443443"}
                                            }
                                        }
                                    }
                                }
                            },
                            {
                                "account": {
                                    "data": {
                                        "parsed": {
                                            "info": {
                                                "tokenAmount": {"amount": "556557"}
                                            }
                                        }
                                    }
                                }
                            },
                        ],
                    }
                ),
            )
            usdc = await client.get_token_balance(PAYER_PUBKEY, USDC_MINT)

    assert lamports == 1_500_000_000
    # Summed across accounts, not just the canonical ATA.
    assert usdc == 9_000_000


async def test_get_token_balance_zero_when_no_account(settings_factory):
    """No token account is 'owns none', the expected pre-first-buy state."""
    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(_RPC_URL, payload=_rpc_result({"context": {}, "value": []}))
            assert await client.get_token_balance(PAYER_PUBKEY, USDC_MINT) == 0


async def test_get_latest_blockhash(settings_factory):
    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _RPC_URL,
                payload=_rpc_result(
                    {
                        "context": {"slot": 283_000_100},
                        "value": {
                            "blockhash": "EETubP5AKHgjPAhzPAFcb8BAY1hMH639CWCFTqi3hq1k",
                            "lastValidBlockHeight": 283_000_500,
                        },
                    }
                ),
            )
            blockhash, last_valid = await client.get_latest_blockhash()

    assert blockhash == "EETubP5AKHgjPAhzPAFcb8BAY1hMH639CWCFTqi3hq1k"
    assert last_valid == 283_000_500


async def test_get_block_height_is_height_not_slot(settings_factory):
    """lastValidBlockHeight is denominated in HEIGHT; comparing a slot to it
    would expire transactions early (skipped slots do not advance height)."""
    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(_RPC_URL, payload=_rpc_result(282_999_990))
            height = await client.get_block_height()
            key = next(iter(mock.requests))
            body = mock.requests[key][0].kwargs["json"]

    assert height == 282_999_990
    assert body["method"] == "getBlockHeight"


async def test_get_transaction_returns_none_when_absent(settings_factory):
    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(_RPC_URL, payload=_rpc_result(None))
            assert await client.get_transaction(_SIGNATURE) is None


async def test_get_transaction_returns_record(settings_factory):
    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _RPC_URL,
                payload=_rpc_result(
                    {
                        "slot": 283_000_455,
                        "meta": {"err": None, "fee": 105_200},
                        "blockTime": 1_785_000_000,
                    }
                ),
            )
            record = await client.get_transaction(_SIGNATURE)
            key = next(iter(mock.requests))
            body = mock.requests[key][0].kwargs["json"]

    assert record["meta"]["fee"] == 105_200
    # v0 transactions are invisible without this, and the lane only builds v0.
    assert body["params"][1]["maxSupportedTransactionVersion"] == 0


async def test_rpc_json_rpc_error_is_definitive(settings_factory):
    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _RPC_URL,
                payload={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32602, "message": "bad"},
                },
            )
            with pytest.raises(SolanaAPIError, match="bad"):
                await client.get_block_height()


async def test_fetch_address_lookup_tables_decodes_addresses(settings_factory):
    import base58
    from solders.pubkey import Pubkey

    addresses = [
        Pubkey.from_string("So11111111111111111111111111111111111111112"),
        Pubkey.from_string(USDC_MINT),
    ]
    raw = bytes(LOOKUP_TABLE_META_SIZE) + b"".join(bytes(a) for a in addresses)
    table_key = "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"

    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _RPC_URL,
                payload=_rpc_result(
                    {
                        "context": {},
                        "value": [{"data": [base64.b64encode(raw).decode(), "base64"]}],
                    }
                ),
            )
            tables = await client.fetch_address_lookup_tables([table_key])

    assert tables[table_key] == [str(a) for a in addresses]
    assert base58.b58decode(tables[table_key][0])


async def test_ragged_lookup_table_is_omitted_not_truncated(settings_factory):
    """A trailing partial pubkey means we cannot trust the table at all."""
    raw = bytes(LOOKUP_TABLE_META_SIZE) + b"\x01" * 40  # 40 is not a multiple of 32
    table_key = "3AVi9Tg9Uo68tJfuvoKvqKNWKkC5wPdSSdeBnizKZ6jT"

    async with aiohttp.ClientSession() as session:
        client = SolanaRpcClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _RPC_URL,
                payload=_rpc_result(
                    {
                        "context": {},
                        "value": [{"data": [base64.b64encode(raw).decode(), "base64"]}],
                    }
                ),
            )
            tables = await client.fetch_address_lookup_tables([table_key])

    assert table_key not in tables


# ----------------------------------------------------------------------
# Jito submission
# ----------------------------------------------------------------------
async def test_submit_success_returns_receipt(settings_factory):
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _SUBMIT_RE,
                payload={"jsonrpc": "2.0", "id": 1, "result": _SIGNATURE},
                headers={"x-bundle-id": "bundle-abc"},
            )
            receipt = await client.submit_transaction(
                build_swap_tx().tx_b64,
                expected_signature=_SIGNATURE,
                last_valid_block_height=283_000_500,
            )

    assert receipt.signature == _SIGNATURE
    assert receipt.returned_signature == _SIGNATURE
    assert receipt.bundle_id == "bundle-abc"
    # The DEFAULT is now False — revert protection costs landing probability,
    # and this lane covers revert risk with an independent pre-sign simulation.
    assert receipt.bundle_only is False


@pytest.mark.parametrize("configured", [True, False])
async def test_bundle_only_comes_from_config_and_sets_the_query_param(
    settings_factory, configured
):
    """*** It was a hardcoded ``bundle_only: bool = True`` default parameter. ***

    Not settable at all, so the trade-off it encodes could not be changed
    without a code edit. Both directions issue exactly ONE post — the
    anti-double-send property is independent of the routing choice.
    """
    async with aiohttp.ClientSession() as session:
        client = JitoClient(
            settings_factory(SOLANA_JITO_BUNDLE_ONLY=configured), session
        )
        with aioresponses() as mock:
            mock.post(
                _SUBMIT_RE, payload={"jsonrpc": "2.0", "id": 1, "result": _SIGNATURE}
            )
            receipt = await client.submit_transaction(
                build_swap_tx().tx_b64, expected_signature=_SIGNATURE
            )
            key = next(iter(mock.requests))
            posts = mock.requests[key]
            body = posts[0].kwargs["json"]

    assert len(posts) == 1, "a submission must never post twice"
    assert receipt.bundle_only is configured
    assert ("bundleOnly=true" in str(key[1])) is configured
    assert body["method"] == "sendTransaction"
    # Explicit base64: the param default is the deprecated base58.
    assert body["params"][1] == {"encoding": "base64"}


async def test_an_explicit_bundle_only_argument_still_wins(settings_factory):
    """Config is the default, not a cage — a caller may still pin it."""
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(SOLANA_JITO_BUNDLE_ONLY=False), session)
        with aioresponses() as mock:
            mock.post(
                _SUBMIT_RE, payload={"jsonrpc": "2.0", "id": 1, "result": _SIGNATURE}
            )
            receipt = await client.submit_transaction(
                build_swap_tx().tx_b64,
                expected_signature=_SIGNATURE,
                bundle_only=True,
            )
    assert receipt.bundle_only is True


@pytest.mark.parametrize("casing", ["x-bundle-id", "X-Bundle-Id", "X-BUNDLE-ID"])
async def test_the_bundle_id_header_is_read_whatever_its_casing(
    settings_factory, casing
):
    """*** THE REASON PRODUCTION LOGGED bundle_id=None. ***

    The header was always read and the constant was always right. What broke
    it was one layer down: aiohttp's ``resp.headers`` is a case-INSENSITIVE
    CIMultiDict, and copying it into a plain dict silently made the lookup
    case-SENSITIVE. HTTP header names are case-insensitive by spec (RFC 9110
    section 5.1), so the casing is the server's choice and can change without
    notice.

    Every existing test mocked the header lowercase, so the tests encoded the
    same assumption as the code and passed while the live lane went blind.
    """
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _SUBMIT_RE,
                payload={"jsonrpc": "2.0", "id": 1, "result": _SIGNATURE},
                headers={casing: "bundle-xyz"},
            )
            receipt = await client.submit_transaction(
                build_swap_tx().tx_b64, expected_signature=_SIGNATURE
            )
    assert receipt.bundle_id == "bundle-xyz"


async def test_a_missing_bundle_id_header_is_tolerated(settings_factory):
    """Absent is not an error — some responses simply do not carry it, and a
    submission still has to complete and stay resolvable by signature."""
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _SUBMIT_RE, payload={"jsonrpc": "2.0", "id": 1, "result": _SIGNATURE}
            )
            receipt = await client.submit_transaction(
                build_swap_tx().tx_b64, expected_signature=_SIGNATURE
            )
    assert receipt.bundle_id is None
    assert receipt.signature == _SIGNATURE


@pytest.mark.parametrize("casing", ["x-bundle-id", "X-Bundle-Id"])
async def test_the_bundle_id_survives_an_ambiguous_submission(settings_factory, casing):
    """The ambiguous path is where a bundle id is most useful and most easily
    lost — the body never arrived, but the header may still have."""
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _SUBMIT_RE,
                status=503,
                body="busy",
                headers={casing: "b-9"},
                repeat=True,
            )
            with pytest.raises(SolanaAmbiguousSubmissionError) as excinfo:
                await client.submit_transaction(
                    build_swap_tx().tx_b64, expected_signature=_SIGNATURE
                )
    assert excinfo.value.bundle_id == "b-9"
    assert excinfo.value.expected_signature == _SIGNATURE


async def test_submit_timeout_is_ambiguous_and_posts_exactly_once(settings_factory):
    """THE anti-double-send property."""
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(_SUBMIT_RE, exception=asyncio.TimeoutError(), repeat=True)
            with pytest.raises(SolanaAmbiguousSubmissionError) as excinfo:
                await client.submit_transaction(
                    build_swap_tx().tx_b64,
                    expected_signature=_SIGNATURE,
                    last_valid_block_height=283_000_500,
                )
            posts = sum(len(v) for k, v in mock.requests.items() if k[0] == "POST")

    assert posts == 1, f"expected exactly one POST, got {posts}"
    assert excinfo.value.expected_signature == _SIGNATURE
    assert excinfo.value.last_valid_block_height == 283_000_500
    assert "Do NOT rebuild" in str(excinfo.value)


@pytest.mark.parametrize("status", [500, 502, 503, 408, 400])
async def test_submit_http_failures_are_ambiguous_never_definitive(
    settings_factory, status
):
    """Jito is JSON-RPC: a raw HTTP status is transport, not a refusal."""
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(_SUBMIT_RE, status=status, body="upstream said no", repeat=True)
            with pytest.raises(SolanaAmbiguousSubmissionError):
                await client.submit_transaction(
                    build_swap_tx().tx_b64, expected_signature=_SIGNATURE
                )
            posts = sum(len(v) for k, v in mock.requests.items() if k[0] == "POST")

    assert posts == 1


async def test_submit_jsonrpc_error_is_definitive_rejection(settings_factory):
    """An `error` member IS the block engine deciding — not ambiguous."""
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _SUBMIT_RE,
                payload={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {"code": -32602, "message": "invalid transaction"},
                },
            )
            with pytest.raises(SolanaAPIError, match="invalid transaction"):
                await client.submit_transaction(
                    build_swap_tx().tx_b64, expected_signature=_SIGNATURE
                )


async def test_submit_signature_mismatch_is_ambiguous(settings_factory):
    """Jito acknowledged a different transaction than the one we signed."""
    other = "5Rf81Nd6uXrp39hFFr1WMFnGjzxyGXrvwb8VSRUnotH1uTbzLEsatgthdk1mR2yUq2P1Bz5HXoccfQX7YTUgffwy"
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(_SUBMIT_RE, payload={"jsonrpc": "2.0", "id": 1, "result": other})
            with pytest.raises(
                SolanaAmbiguousSubmissionError, match="not the one we signed"
            ):
                await client.submit_transaction(
                    build_swap_tx().tx_b64, expected_signature=_SIGNATURE
                )


async def test_bundle_id_captured_even_on_failure(settings_factory):
    """A 5xx can still carry the bundle id, and it is the only status handle."""
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                _SUBMIT_RE,
                status=503,
                body="busy",
                headers={"x-bundle-id": "b-9"},
                repeat=True,
            )
            with pytest.raises(SolanaAmbiguousSubmissionError):
                await client.submit_transaction(
                    build_swap_tx().tx_b64, expected_signature=_SIGNATURE
                )


async def test_fetch_tip_accounts_prefers_live_list(settings_factory):
    live = ["4ACfpUFoaSD9bfPdeu6DBt89gB6ENTeHBXCAi87NhDEE", TIP_ACCOUNT]
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                f"{_JITO}/api/v1/getTipAccounts",
                payload={"jsonrpc": "2.0", "id": 1, "result": live},
            )
            accounts = await client.fetch_tip_accounts()

    assert accounts == frozenset(live)


async def test_fetch_tip_accounts_falls_back_on_failure(settings_factory):
    """A block-engine outage must not block the pilot on a rarely-changing list."""
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(f"{_JITO}/api/v1/getTipAccounts", status=503, repeat=True)
            accounts = await client.fetch_tip_accounts()

    assert accounts == JITO_TIP_ACCOUNTS_FALLBACK
    assert TIP_ACCOUNT in accounts


async def test_a_transient_failure_walks_the_whole_backoff_ladder(
    settings_factory, solana_retry_backoff
):
    """*** The retry SEMANTICS, asserted now that the waiting is gone. ***

    The `solana_retry_backoff` fixture removes the sleeps, which would be a bad
    trade if it also removed the thing the sleeps were evidence of. It does
    not: the ladder's length still decides the attempt count, and the delays it
    asks for are recorded. So this pins both — four attempts on a retryable
    failure, and the exact schedule walked in order.
    """
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(f"{_JITO}/api/v1/getTipAccounts", status=503, repeat=True)
            await client.fetch_tip_accounts()
            attempts = sum(len(calls) for calls in mock.requests.values())

    # One initial attempt plus one per rung.
    assert attempts == 4
    assert solana_retry_backoff == [0.5, 1.0, 2.0]
    assert list(transport_module._BACKOFFS) == [0.5, 1.0, 2.0]


async def test_a_submission_never_sleeps_because_it_never_retries(
    settings_factory, solana_retry_backoff
):
    """The submit path opts out of the ladder, so it must ask for no delay.

    The anti-double-send property is asserted elsewhere on the POST count; this
    is its companion — a lane that slept would be a lane that had retried.
    """
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(_SUBMIT_RE, exception=asyncio.TimeoutError(), repeat=True)
            with pytest.raises(SolanaAmbiguousSubmissionError):
                await client.submit_transaction(
                    build_swap_tx().tx_b64,
                    expected_signature=_SIGNATURE,
                    last_valid_block_height=283_000_500,
                )
            posts = sum(len(v) for k, v in mock.requests.items() if k[0] == "POST")

    assert posts == 1
    assert solana_retry_backoff == []


async def test_inflight_bundle_statuses_parsed(settings_factory):
    async with aiohttp.ClientSession() as session:
        client = JitoClient(settings_factory(), session)
        with aioresponses() as mock:
            mock.post(
                f"{_JITO}/api/v1/getInflightBundleStatuses",
                payload={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "context": {"slot": 1},
                        "value": [
                            {
                                "bundle_id": "b-1",
                                "status": "Landed",
                                "landed_slot": 283_000_400,
                            }
                        ],
                    },
                },
            )
            statuses = await client.get_inflight_bundle_statuses(["b-1"])

    assert statuses[0].status == "Landed"
    assert statuses[0].landed_slot == 283_000_400
