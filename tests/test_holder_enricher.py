"""Tests for holder enrichment."""

import pytest
import aiohttp
from aioresponses import aioresponses

from scout.ingestion.holder_enricher import enrich_holders


@pytest.fixture
def mock_aiohttp():
    with aioresponses() as m:
        yield m


async def test_enrich_solana_with_helius(mock_aiohttp, token_factory, settings_factory):
    token = token_factory(chain="solana", contract_address="SoLAddr123")
    settings = settings_factory(HELIUS_API_KEY="test-helius-key")

    # Mock Helius DAS API getTokenAccounts
    mock_aiohttp.post(
        "https://mainnet.helius-rpc.com/?api-key=test-helius-key",
        payload={"result": {"total": 450, "items": []}},
    )

    async with aiohttp.ClientSession() as session:
        enriched = await enrich_holders(token, session, settings)

    assert enriched.holder_count == 450


async def test_enrich_evm_with_moralis(mock_aiohttp, token_factory, settings_factory):
    token = token_factory(chain="ethereum", contract_address="0xEvmAddr")
    settings = settings_factory(MORALIS_API_KEY="test-moralis-key")

    mock_aiohttp.get(
        "https://deep-index.moralis.io/api/v2.2/erc20/0xEvmAddr/owners?chain=eth",
        payload={"result": [{"owner": "0x1"}, {"owner": "0x2"}], "cursor": None},
        headers={"X-API-Key": "test-moralis-key"},
    )

    async with aiohttp.ClientSession() as session:
        enriched = await enrich_holders(token, session, settings)

    assert enriched.holder_count == 2


async def test_enrich_no_api_key_returns_unenriched(
    mock_aiohttp, token_factory, settings_factory
):
    """Graceful degradation: no API key -> return token unchanged."""
    token = token_factory(chain="solana", holder_count=0, holder_growth_1h=0)
    settings = settings_factory()  # No HELIUS_API_KEY set

    async with aiohttp.ClientSession() as session:
        enriched = await enrich_holders(token, session, settings)

    assert enriched.holder_count == 0  # unchanged
    assert enriched.holder_growth_1h == 0


async def test_enrich_api_failure_returns_unenriched(
    mock_aiohttp, token_factory, settings_factory
):
    """API failure -> return token unchanged, don't crash."""
    token = token_factory(
        chain="solana", contract_address="SoLAddr", holder_count=0, holder_growth_1h=0
    )
    settings = settings_factory(HELIUS_API_KEY="bad-key")

    mock_aiohttp.post(
        "https://mainnet.helius-rpc.com/?api-key=bad-key",
        status=500,
    )

    async with aiohttp.ClientSession() as session:
        enriched = await enrich_holders(token, session, settings)

    assert enriched.holder_count == 0  # unchanged, graceful degradation
