"""PR-S1: Jupiter client — quote parsing, fail-closed behaviour, swap build.

Payload shapes follow Jupiter's published schema
(https://developers.jup.ag/docs/api-reference/swap/quote and .../swap):
amounts are decimal STRINGS, slippageBps is a number, and the swap response
carries swapTransaction (base64) + lastValidBlockHeight.
"""

from __future__ import annotations

import re

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.live.solana.constants import SOL_MINT, USDC_MINT
from scout.live.solana.exceptions import SolanaAPIError, SolanaResponseError
from scout.live.solana.jupiter_client import JupiterClient
from solana_tx_builder import PAYER_PUBKEY, build_swap_tx

_QUOTE_RE = re.compile(r"https://api\.jup\.ag/swap/v1/quote.*")
_SWAP_URL = "https://api.jup.ag/swap/v1/swap"


def _quote_payload(**overrides):
    payload = {
        "inputMint": SOL_MINT,
        "inAmount": "50000000",
        "outputMint": USDC_MINT,
        "outAmount": "8528730",
        "otherAmountThreshold": "8443443",
        "swapMode": "ExactIn",
        "slippageBps": 100,
        "priceImpactPct": "0.0001",
        "contextSlot": 283_000_111,
        "timeTaken": 0.012,
        "routePlan": [
            {
                "swapInfo": {
                    "ammKey": "58oQChx4yWmvKdwLLZzBi4ChoCc2fqCUWBkwMihLYQo2",
                    "label": "Raydium",
                    "inputMint": SOL_MINT,
                    "outputMint": USDC_MINT,
                    "inAmount": "50000000",
                    "outAmount": "8528730",
                    "feeAmount": "12500",
                    "feeMint": SOL_MINT,
                },
                "percent": 100,
            }
        ],
    }
    payload.update(overrides)
    return payload


def _swap_payload(**overrides):
    payload = {
        "swapTransaction": build_swap_tx().tx_b64,
        "lastValidBlockHeight": 283_000_500,
        "prioritizationFeeLamports": 100_200,
    }
    payload.update(overrides)
    return payload


async def _client(settings, **kw):
    session = aiohttp.ClientSession(**kw)
    return JupiterClient(settings, session), session


# ----------------------------------------------------------------------
# get_quote
# ----------------------------------------------------------------------
async def test_get_quote_parses_every_field(settings_factory):
    client, session = await _client(settings_factory())
    async with session:
        with aioresponses() as mock:
            mock.get(_QUOTE_RE, payload=_quote_payload())
            quote = await client.get_quote(amount=50_000_000)

    assert quote.in_amount == 50_000_000
    assert quote.out_amount == 8_528_730
    assert quote.other_amount_threshold == 8_443_443
    # The on-chain floor, not the estimate — what the approval screen shows.
    assert quote.min_out_amount == 8_443_443
    assert quote.slippage_bps == 100
    assert quote.swap_mode == "ExactIn"
    assert quote.price_impact_pct == "0.0001"
    assert quote.context_slot == 283_000_111
    assert len(quote.route_plan) == 1
    assert quote.route_plan[0].label == "Raydium"
    assert quote.route_plan[0].in_amount == 50_000_000
    # raw must be preserved verbatim for the /swap echo.
    assert quote.raw["otherAmountThreshold"] == "8443443"


async def test_get_quote_sends_documented_parameters(settings_factory):
    client, session = await _client(settings_factory(SOLANA_PILOT_SLIPPAGE_BPS=75))
    async with session:
        with aioresponses() as mock:
            mock.get(_QUOTE_RE, payload=_quote_payload(slippageBps=75))
            await client.get_quote(amount=1_000_000)
            request_key = next(iter(mock.requests))
            url = str(request_key[1])

    assert f"inputMint={SOL_MINT}" in url
    assert f"outputMint={USDC_MINT}" in url
    assert "amount=1000000" in url
    assert "slippageBps=75" in url
    assert "restrictIntermediateTokens=true" in url


@pytest.mark.parametrize(
    "missing",
    [
        "outAmount",
        "otherAmountThreshold",
        "inAmount",
        "routePlan",
        "priceImpactPct",
        "swapMode",
        "slippageBps",
    ],
)
async def test_get_quote_fails_closed_on_missing_field(settings_factory, missing):
    payload = _quote_payload()
    payload.pop(missing)
    client, session = await _client(settings_factory())
    async with session:
        with aioresponses() as mock:
            mock.get(_QUOTE_RE, payload=payload)
            with pytest.raises(SolanaResponseError, match=missing):
                await client.get_quote(amount=50_000_000)


async def test_get_quote_rejects_empty_route_plan(settings_factory):
    client, session = await _client(settings_factory())
    async with session:
        with aioresponses() as mock:
            mock.get(_QUOTE_RE, payload=_quote_payload(routePlan=[]))
            with pytest.raises(SolanaResponseError, match="routePlan is empty"):
                await client.get_quote(amount=50_000_000)


async def test_get_quote_rejects_mint_mismatch(settings_factory):
    """A route that does not connect the mints we asked for is not our swap."""
    decoy = "7vfCXTUXx5WJV5JADk17DUJ4ksgau7utNKj4b963voxs"
    client, session = await _client(settings_factory())
    async with session:
        with aioresponses() as mock:
            mock.get(_QUOTE_RE, payload=_quote_payload(outputMint=decoy))
            with pytest.raises(SolanaResponseError, match="mint mismatch"):
                await client.get_quote(amount=50_000_000)


async def test_get_quote_rejects_non_integer_amount(settings_factory):
    client, session = await _client(settings_factory())
    async with session:
        with aioresponses() as mock:
            mock.get(_QUOTE_RE, payload=_quote_payload(outAmount="8528730.5"))
            with pytest.raises(SolanaResponseError, match="outAmount"):
                await client.get_quote(amount=50_000_000)


async def test_get_quote_http_400_is_definitive(settings_factory):
    """Jupiter is REST: a 4xx IS the API refusing, not ambiguous transport."""
    client, session = await _client(settings_factory())
    async with session:
        with aioresponses() as mock:
            mock.get(_QUOTE_RE, status=400, body='{"error":"No route found"}')
            with pytest.raises(SolanaAPIError, match="No route found"):
                await client.get_quote(amount=50_000_000)


async def test_get_quote_rejects_non_positive_amount(settings_factory):
    client, session = await _client(settings_factory())
    async with session:
        with pytest.raises(ValueError, match="must be > 0"):
            await client.get_quote(amount=0)


# ----------------------------------------------------------------------
# build_swap_transaction
# ----------------------------------------------------------------------
async def _get_quote(client, mock):
    mock.get(_QUOTE_RE, payload=_quote_payload())
    return await client.get_quote(amount=50_000_000)


async def test_build_swap_sends_jito_tip_variant(settings_factory):
    """The tip must be a BARE INTEGER under jitoTipLamports.

    Jupiter's prioritizationFeeLamports is a oneOf; nesting the tip in an
    object is a different variant and would silently produce a transaction
    with no Jito tip — which then never wins the auction.
    """
    client, session = await _client(settings_factory())
    async with session:
        with aioresponses() as mock:
            quote = await _get_quote(client, mock)
            mock.post(_SWAP_URL, payload=_swap_payload())
            build = await client.build_swap_transaction(
                quote=quote, user_pubkey=PAYER_PUBKEY, jito_tip_lamports=100_000
            )

            post_key = [k for k in mock.requests if k[0] == "POST"][0]
            body = mock.requests[post_key][0].kwargs["json"]

    assert body["prioritizationFeeLamports"] == {"jitoTipLamports": 100_000}
    assert body["userPublicKey"] == PAYER_PUBKEY
    # The quote is echoed VERBATIM, not rebuilt.
    assert body["quoteResponse"] == quote.raw
    assert body["wrapAndUnwrapSol"] is True
    assert body["dynamicComputeUnitLimit"] is True
    assert build.last_valid_block_height == 283_000_500
    assert build.prioritization_fee_lamports == 100_200
    assert build.requested_jito_tip_lamports == 100_000


async def test_build_swap_round_trips_transaction_b64(settings_factory):
    expected = build_swap_tx().tx_b64
    client, session = await _client(settings_factory())
    async with session:
        with aioresponses() as mock:
            quote = await _get_quote(client, mock)
            mock.post(_SWAP_URL, payload=_swap_payload(swapTransaction=expected))
            build = await client.build_swap_transaction(
                quote=quote, user_pubkey=PAYER_PUBKEY, jito_tip_lamports=1_000
            )

    assert build.swap_transaction_b64 == expected


@pytest.mark.parametrize("missing", ["swapTransaction", "lastValidBlockHeight"])
async def test_build_swap_fails_closed_on_missing_field(settings_factory, missing):
    payload = _swap_payload()
    payload.pop(missing)
    client, session = await _client(settings_factory())
    async with session:
        with aioresponses() as mock:
            quote = await _get_quote(client, mock)
            mock.post(_SWAP_URL, payload=payload)
            with pytest.raises(SolanaResponseError, match=missing):
                await client.build_swap_transaction(
                    quote=quote, user_pubkey=PAYER_PUBKEY, jito_tip_lamports=1_000
                )


async def test_build_swap_tolerates_absent_optional_fields(settings_factory):
    """computeUnitLimit/prioritizationFeeLamports are not in the schema's
    required set, so their absence must not fail the build."""
    client, session = await _client(settings_factory())
    async with session:
        with aioresponses() as mock:
            quote = await _get_quote(client, mock)
            mock.post(
                _SWAP_URL,
                payload={
                    "swapTransaction": build_swap_tx().tx_b64,
                    "lastValidBlockHeight": 283_000_500,
                },
            )
            build = await client.build_swap_transaction(
                quote=quote, user_pubkey=PAYER_PUBKEY, jito_tip_lamports=1_000
            )

    assert build.prioritization_fee_lamports is None
    assert build.compute_unit_limit is None
    assert build.simulation_error is None


async def test_build_swap_refuses_tip_over_ceiling_before_any_request(settings_factory):
    """No point asking Jupiter to build what we have decided not to sign."""
    client, session = await _client(
        settings_factory(SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=50_000)
    )
    async with session:
        with aioresponses() as mock:
            quote = await _get_quote(client, mock)
            with pytest.raises(ValueError, match="exceeds"):
                await client.build_swap_transaction(
                    quote=quote, user_pubkey=PAYER_PUBKEY, jito_tip_lamports=60_000
                )
            assert not [k for k in mock.requests if k[0] == "POST"]


async def test_api_key_is_sent_as_header_when_configured(settings_factory):
    client, session = await _client(settings_factory(JUPITER_API_KEY="jup-secret"))
    async with session:
        with aioresponses() as mock:
            mock.get(_QUOTE_RE, payload=_quote_payload())
            await client.get_quote(amount=50_000_000)
            key = next(iter(mock.requests))
            headers = mock.requests[key][0].kwargs.get("headers") or {}

    assert headers.get("x-api-key") == "jup-secret"


async def test_blank_api_key_is_treated_as_absent(settings_factory):
    """Sending `x-api-key: ""` gets rejected rather than falling back."""
    client, session = await _client(settings_factory(JUPITER_API_KEY=""))
    async with session:
        with aioresponses() as mock:
            mock.get(_QUOTE_RE, payload=_quote_payload())
            await client.get_quote(amount=50_000_000)
            key = next(iter(mock.requests))
            headers = mock.requests[key][0].kwargs.get("headers") or {}

    assert "x-api-key" not in headers
