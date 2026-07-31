"""PR-S1: the venue-client modules composed end to end.

Each module is unit-tested in isolation elsewhere. This file proves they fit
together in the order the PR-S2 runner will call them:

    quote -> build -> inspect -> simulate -> sign -> submit -> resolve

and, critically, that the signature derived at the SIGN step is the one the
RESOLVE step looks up. A seam bug between those two would leave every
ambiguous submission unresolvable while every unit test still passed.
"""

from __future__ import annotations

import re

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.live.solana.constants import SOL_MINT, USDC_MINT
from scout.live.solana.exceptions import SolanaAmbiguousSubmissionError
from scout.live.solana.jito_client import JitoClient
from scout.live.solana.jupiter_client import JupiterClient
from scout.live.solana.resolver import resolve_submission
from scout.live.solana.rpc_client import SolanaRpcClient
from scout.live.solana.signer import sign_transaction
from scout.live.solana.tx_inspector import verify_swap_transaction
from solana_tx_builder import PAYER, PAYER_PUBKEY, STRANGER, build_swap_tx

_QUOTE_RE = re.compile(r"https://api\.jup\.ag/swap/v1/quote.*")
_SWAP_URL = "https://api.jup.ag/swap/v1/swap"
_RPC_URL = "https://api.mainnet-beta.solana.com"
_SUBMIT_RE = re.compile(
    r"https://mainnet\.block-engine\.jito\.wtf/api/v1/transactions.*"
)

_LAST_VALID = 283_000_500


def _quote_payload():
    return {
        "inputMint": SOL_MINT,
        "inAmount": "50000000",
        "outputMint": USDC_MINT,
        "outAmount": "8528730",
        "otherAmountThreshold": "8443443",
        "swapMode": "ExactIn",
        "slippageBps": 100,
        "priceImpactPct": "0.0001",
        "contextSlot": 283_000_111,
        "routePlan": [
            {
                "swapInfo": {
                    "ammKey": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
                    "label": "Raydium",
                    "inputMint": SOL_MINT,
                    "outputMint": USDC_MINT,
                    "inAmount": "50000000",
                    "outAmount": "8528730",
                },
                "percent": 100,
            }
        ],
    }


def _rpc_result(value):
    return {"jsonrpc": "2.0", "id": 1, "result": value}


async def test_full_lane_quote_to_submission(settings_factory):
    settings = settings_factory()
    tx_b64 = build_swap_tx().tx_b64

    async with aiohttp.ClientSession() as session:
        jupiter = JupiterClient(settings, session)
        rpc = SolanaRpcClient(settings, session)
        jito = JitoClient(settings, session)

        with aioresponses() as mock:
            mock.get(_QUOTE_RE, payload=_quote_payload())
            quote = await jupiter.get_quote(amount=50_000_000)

            mock.post(
                _SWAP_URL,
                payload={
                    "swapTransaction": tx_b64,
                    "lastValidBlockHeight": _LAST_VALID,
                    "prioritizationFeeLamports": 200,
                },
            )
            build = await jupiter.build_swap_transaction(
                quote=quote, user_pubkey=PAYER_PUBKEY, jito_tip_lamports=100_000
            )

            report = await verify_swap_transaction(
                tx_b64=build.swap_transaction_b64,
                expected_signer=PAYER_PUBKEY,
                settings=settings,
                rpc_client=rpc,
            )
            report.raise_if_failed()

            # Pre-sign simulation: the transaction need not be signed.
            mock.post(
                _RPC_URL,
                payload=_rpc_result(
                    {
                        "context": {"slot": 283_000_111},
                        "value": {"err": None, "logs": [], "unitsConsumed": 84_000},
                    }
                ),
            )
            sim = await rpc.simulate_transaction(build.swap_transaction_b64)
            SolanaRpcClient.raise_for_simulation(sim)

            signed = sign_transaction(
                build.swap_transaction_b64, PAYER, expected_signer=PAYER_PUBKEY
            )

            mock.post(
                _SUBMIT_RE,
                payload={"jsonrpc": "2.0", "id": 1, "result": signed.signature},
                headers={"x-bundle-id": "bundle-xyz"},
            )
            receipt = await jito.submit_transaction(
                signed.signed_tx_b64,
                expected_signature=signed.signature,
                last_valid_block_height=build.last_valid_block_height,
            )

            # Resolve using the signature derived BEFORE submission.
            mock.post(
                _RPC_URL,
                payload=_rpc_result(
                    {
                        "context": {"slot": 283_000_460},
                        "value": [
                            {
                                "slot": 283_000_455,
                                "confirmations": None,
                                "err": None,
                                "confirmationStatus": "finalized",
                            }
                        ],
                    }
                ),
            )
            resolution = await resolve_submission(
                expected_signature=receipt.signature,
                last_valid_block_height=build.last_valid_block_height,
                rpc_client=rpc,
                settings=settings,
            )

    # The seam that matters: signed -> submitted -> resolved, one signature.
    assert receipt.signature == signed.signature
    assert resolution.signature == signed.signature
    assert resolution.verdict == "landed"
    assert resolution.rebuild_is_safe is False
    assert report.message_sha256 == signed.message_sha256
    assert quote.min_out_amount == 8_443_443


async def test_lane_refuses_to_sign_a_transaction_that_fails_inspection(
    settings_factory,
):
    """The inspector is a gate, not a report: a bad build never reaches the key."""
    settings = settings_factory()
    hostile = build_swap_tx(extra_transfer_dest=STRANGER).tx_b64

    async with aiohttp.ClientSession() as session:
        jupiter = JupiterClient(settings, session)
        with aioresponses() as mock:
            mock.get(_QUOTE_RE, payload=_quote_payload())
            quote = await jupiter.get_quote(amount=50_000_000)
            mock.post(
                _SWAP_URL,
                payload={
                    "swapTransaction": hostile,
                    "lastValidBlockHeight": _LAST_VALID,
                },
            )
            build = await jupiter.build_swap_transaction(
                quote=quote, user_pubkey=PAYER_PUBKEY, jito_tip_lamports=100_000
            )

            report = await verify_swap_transaction(
                tx_b64=build.swap_transaction_b64,
                expected_signer=PAYER_PUBKEY,
                settings=settings,
            )

    assert not report.passed
    assert "transfer_destinations_known" in {c.name for c in report.failures}


async def test_ambiguous_submission_is_resolvable_from_the_derived_signature(
    settings_factory,
):
    """The whole point of deriving the signature before submitting.

    The POST times out, so the block engine told us nothing at all — yet the
    transaction is still identifiable, and here it turns out to have landed.
    A lane that only learned its signature from the response would have to
    guess, and guessing 'it did not land' is how a supervised trade becomes
    two trades.
    """
    settings = settings_factory()
    signed = sign_transaction(build_swap_tx().tx_b64, PAYER)

    async with aiohttp.ClientSession() as session:
        jito = JitoClient(settings, session)
        rpc = SolanaRpcClient(settings, session)

        with aioresponses() as mock:
            mock.post(_SUBMIT_RE, exception=TimeoutError(), repeat=True)
            with pytest.raises(SolanaAmbiguousSubmissionError) as excinfo:
                await jito.submit_transaction(
                    signed.signed_tx_b64,
                    expected_signature=signed.signature,
                    last_valid_block_height=_LAST_VALID,
                )

            assert excinfo.value.expected_signature == signed.signature

            mock.post(
                _RPC_URL,
                payload=_rpc_result(
                    {
                        "context": {"slot": 283_000_460},
                        "value": [
                            {
                                "slot": 283_000_455,
                                "confirmations": 12,
                                "err": None,
                                "confirmationStatus": "confirmed",
                            }
                        ],
                    }
                ),
            )
            resolution = await resolve_submission(
                expected_signature=excinfo.value.expected_signature,
                last_valid_block_height=excinfo.value.last_valid_block_height,
                rpc_client=rpc,
                settings=settings,
            )

    assert resolution.verdict == "landed"
    assert resolution.rebuild_is_safe is False
