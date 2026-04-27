"""BL-064 resolver state-machine tests.

Covers the RESOLVED / UNRESOLVED_TRANSIENT / UNRESOLVED_TERMINAL paths
exercised by the listener's retry logic. Uses aioresponses so we don't
hit CG/DexScreener for real.
"""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses

from scout.social.telegram.models import (
    ContractRef,
    ResolutionState,
)
from scout.social.telegram.resolver import (
    CG_BASE,
    DEXSCREENER_BASE,
    _Outcome,
    resolve_and_enrich,
)


def _cg_contract_url(platform: str, addr: str) -> str:
    return f"{CG_BASE}/coins/{platform}/contract/{addr}"


@pytest.mark.asyncio
async def test_resolves_via_cg_when_present(settings_factory):
    s = settings_factory()
    ref = ContractRef(chain="ethereum", address="0xAbc")
    payload = {
        "id": "test-coin",
        "symbol": "TST",
        "market_data": {
            "market_cap": {"usd": 5_000_000.0},
            "current_price": {"usd": 1.23},
            "total_volume": {"usd": 100_000.0},
        },
    }
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(_cg_contract_url("ethereum", "0xAbc"), payload=payload)
            result = await resolve_and_enrich(
                [ref], [], session=session, settings=s, is_retry=False
            )
    assert result.state == ResolutionState.RESOLVED
    assert len(result.tokens) == 1
    tok = result.tokens[0]
    assert tok.token_id == "test-coin"
    assert tok.symbol == "TST"
    assert tok.mcap == pytest.approx(5_000_000.0)


@pytest.mark.asyncio
async def test_404_promotes_directly_to_terminal_no_retry(settings_factory):
    """404 on CG + 404 on DexScreener → UNRESOLVED_TERMINAL on first pass.
    Closes round-2 Medium #6 — 404 is not transient; retry is wasted."""
    s = settings_factory()
    ref = ContractRef(chain="ethereum", address="0xAbc")
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(_cg_contract_url("ethereum", "0xAbc"), status=404)
            m.get(f"{DEXSCREENER_BASE}/0xAbc", status=404)
            result = await resolve_and_enrich(
                [ref], [], session=session, settings=s, is_retry=False
            )
    assert result.state == ResolutionState.UNRESOLVED_TERMINAL


@pytest.mark.asyncio
async def test_5xx_promotes_to_transient_on_first_pass(settings_factory):
    """5xx → UNRESOLVED_TRANSIENT (retry might help)."""
    s = settings_factory()
    ref = ContractRef(chain="ethereum", address="0xAbc")
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(_cg_contract_url("ethereum", "0xAbc"), status=503)
            m.get(f"{DEXSCREENER_BASE}/0xAbc", status=503)
            result = await resolve_and_enrich(
                [ref], [], session=session, settings=s, is_retry=False
            )
    assert result.state == ResolutionState.UNRESOLVED_TRANSIENT


@pytest.mark.asyncio
async def test_5xx_on_retry_promotes_to_terminal(settings_factory):
    """Same 5xx on the retry pass → UNRESOLVED_TERMINAL (not infinite)."""
    s = settings_factory()
    ref = ContractRef(chain="ethereum", address="0xAbc")
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(_cg_contract_url("ethereum", "0xAbc"), status=503)
            m.get(f"{DEXSCREENER_BASE}/0xAbc", status=503)
            result = await resolve_and_enrich(
                [ref], [], session=session, settings=s, is_retry=True
            )
    assert result.state == ResolutionState.UNRESOLVED_TERMINAL


@pytest.mark.asyncio
async def test_dexscreener_re_attributes_chain_from_chainid(settings_factory):
    """DexScreener returns chainId per pair; resolver must re-attribute the
    token's chain field rather than trusting the parser's default 'ethereum'.
    Closes round-2 Medium #2 — Optimism/BSC/Avalanche CAs were going to GoPlus
    with chain='ethereum' and producing wrong verdicts."""
    s = settings_factory()
    # Parser tags this 0x address as 'ethereum' by default
    ref = ContractRef(chain="ethereum", address="0xAbc")
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            # CG misses (404)
            m.get(_cg_contract_url("ethereum", "0xAbc"), status=404)
            # DexScreener says it's actually on Optimism
            m.get(
                f"{DEXSCREENER_BASE}/0xAbc",
                payload={
                    "pairs": [
                        {
                            "chainId": "optimism",
                            "baseToken": {"symbol": "OPMEME"},
                            "fdv": 250_000.0,
                            "priceUsd": "0.0023",
                            "volume": {"h24": 5000.0},
                            "liquidity": {"usd": 10_000.0},
                        }
                    ]
                },
            )
            result = await resolve_and_enrich(
                [ref], [], session=session, settings=s, is_retry=False
            )
    assert result.state == ResolutionState.RESOLVED
    assert result.tokens[0].chain == "optimism"  # NOT 'ethereum'
    assert result.tokens[0].symbol == "OPMEME"


@pytest.mark.asyncio
async def test_cashtag_only_safety_skipped_no_ca(settings_factory):
    """Cashtag-only resolution must set safety_skipped_no_ca=True so the
    alerter doesn't render misleading 'FAILED safety check' badge.
    Closes round-2 SHOWSTOPPER #4."""
    s = settings_factory()
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                f"{CG_BASE}/search?query=WIF",
                payload={"coins": [{"id": "wif"}, {"id": "wif-2"}]},
            )
            m.get(
                f"{CG_BASE}/coins/markets?vs_currency=usd&ids=wif,wif-2&per_page=10",
                payload=[
                    {
                        "id": "wif",
                        "symbol": "wif",
                        "market_cap": 5_000_000_000.0,
                        "current_price": 0.5,
                        "total_volume": 100_000.0,
                    },
                    {
                        "id": "wif-2",
                        "symbol": "wif",
                        "market_cap": 50_000.0,
                        "current_price": 0.001,
                        "total_volume": 10.0,
                    },
                ],
            )
            result = await resolve_and_enrich(
                [], ["WIF"], session=session, settings=s, is_retry=False
            )
    assert result.state == ResolutionState.RESOLVED
    assert result.tokens == []  # cashtag-only never trade-eligible
    assert len(result.candidates_top3) == 2
    for c in result.candidates_top3:
        assert c.safety_skipped_no_ca is True
        assert c.safety_pass is False
        assert c.safety_check_completed is False


@pytest.mark.asyncio
async def test_cashtag_404_promotes_to_terminal(settings_factory):
    """No CG search match (empty coins) → UNRESOLVED_TERMINAL — search
    didn't fail, the ticker just doesn't exist; no point retrying."""
    s = settings_factory()
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(f"{CG_BASE}/search?query=UNKNOWNTOKEN", payload={"coins": []})
            result = await resolve_and_enrich(
                [], ["UNKNOWNTOKEN"], session=session, settings=s, is_retry=False
            )
    assert result.state == ResolutionState.UNRESOLVED_TERMINAL


@pytest.mark.asyncio
async def test_cashtag_5xx_promotes_to_transient(settings_factory):
    s = settings_factory()
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(f"{CG_BASE}/search?query=WIF", status=503)
            result = await resolve_and_enrich(
                [], ["WIF"], session=session, settings=s, is_retry=False
            )
    assert result.state == ResolutionState.UNRESOLVED_TRANSIENT
