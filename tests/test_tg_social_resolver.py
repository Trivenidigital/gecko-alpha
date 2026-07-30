"""BL-064 resolver state-machine tests.

Covers the RESOLVED / UNRESOLVED_TRANSIENT / UNRESOLVED_TERMINAL paths
exercised by the listener's retry logic, plus the CoinGecko tier/auth
routing (demo vs pro host, key injection). Uses aioresponses so we don't
hit CG/DexScreener for real.
"""

from __future__ import annotations

import aiohttp
import pytest
from aioresponses import aioresponses

from scout import cg_api
from scout.social.telegram.models import (
    ContractRef,
    ResolutionState,
)
from scout.social.telegram.resolver import (
    DEXSCREENER_BASE,
    _Outcome,
    _cg_base,
    _get_json,
    resolve_and_enrich,
)

# settings_factory defaults to tier="demo" with an empty key, so the
# pre-existing state-machine cases pin the keyless demo host.
CG_DEMO = cg_api.DEMO_BASE
CG_PRO = cg_api.PRO_BASE


def _cg_contract_url(platform: str, addr: str, base: str = CG_DEMO) -> str:
    return f"{base}/coins/{platform}/contract/{addr}"


def _requested_urls(m: aioresponses) -> list[str]:
    """Every URL aioresponses actually saw, as strings."""
    return [str(url) for _method, url in m.requests]


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
                f"{CG_DEMO}/search?query=WIF",
                payload={"coins": [{"id": "wif"}, {"id": "wif-2"}]},
            )
            m.get(
                f"{CG_DEMO}/coins/markets?vs_currency=usd&ids=wif,wif-2&per_page=10",
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
            m.get(f"{CG_DEMO}/search?query=UNKNOWNTOKEN", payload={"coins": []})
            result = await resolve_and_enrich(
                [], ["UNKNOWNTOKEN"], session=session, settings=s, is_retry=False
            )
    assert result.state == ResolutionState.UNRESOLVED_TERMINAL


@pytest.mark.asyncio
async def test_cashtag_5xx_promotes_to_transient(settings_factory):
    s = settings_factory()
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(f"{CG_DEMO}/search?query=WIF", status=503)
            result = await resolve_and_enrich(
                [], ["WIF"], session=session, settings=s, is_retry=False
            )
    assert result.state == ResolutionState.UNRESOLVED_TRANSIENT


def _install_fake_limiter(monkeypatch) -> dict[str, int]:
    """Swap the module limiter for a counter; returns the counter dict."""
    calls = {"acquire": 0, "report_429": 0}

    class FakeLimiter:
        async def acquire(self):
            calls["acquire"] += 1

        async def report_429(self):
            calls["report_429"] += 1

    monkeypatch.setattr(
        "scout.social.telegram.resolver.coingecko_limiter",
        FakeLimiter(),
        raising=False,
    )
    return calls


@pytest.mark.asyncio
async def test_resolver_coingecko_429_uses_shared_limiter(
    monkeypatch, settings_factory
):
    """Resolver CoinGecko calls share the global CG limiter and report 429s."""
    s = settings_factory()
    calls = _install_fake_limiter(monkeypatch)

    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(f"{CG_DEMO}/search?query=WIF", status=429)
            outcome, data = await _get_json(
                session, f"{CG_DEMO}/search", s, params={"query": "WIF"}
            )

    assert outcome == _Outcome.TRANSIENT
    assert data is None
    assert calls == {"acquire": 1, "report_429": 1}


# --------------------------------------------- CG tier routing + key injection
#
# The resolver used to hardcode the free/Demo host and send no key at all, so
# every request landed on the keyless public tier and got 429-throttled in
# prod. Host + auth now come from scout.cg_api via Settings.


@pytest.mark.asyncio
async def test_cg_contract_lookup_uses_pro_host_and_key(settings_factory):
    """tier=pro routes the by-contract lookup to pro-api.coingecko.com and
    authenticates with x_cg_pro_api_key (a Demo-host call would give 10010)."""
    s = settings_factory(COINGECKO_API_KEY="CG-prokey", COINGECKO_API_TIER="pro")
    ref = ContractRef(chain="ethereum", address="0xAbc")
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                f"{_cg_contract_url('ethereum', '0xAbc', CG_PRO)}"
                "?x_cg_pro_api_key=CG-prokey",
                payload={
                    "id": "test-coin",
                    "symbol": "TST",
                    "market_data": {"market_cap": {"usd": 1_000.0}},
                },
            )
            result = await resolve_and_enrich(
                [ref], [], session=session, settings=s, is_retry=False
            )
    assert result.state == ResolutionState.RESOLVED
    assert result.tokens[0].token_id == "test-coin"
    requested = _requested_urls(m)
    assert any(u.startswith(CG_PRO) for u in requested)
    assert not any(u.startswith(CG_DEMO) for u in requested)


@pytest.mark.asyncio
async def test_cg_search_uses_demo_host_and_key_on_demo_tier(settings_factory):
    """tier=demo (default) keeps the demo host but must still send the key —
    the keyless public tier is what was getting 429-throttled."""
    s = settings_factory(COINGECKO_API_KEY="CG-demokey")
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                f"{CG_DEMO}/search?query=WIF&x_cg_demo_api_key=CG-demokey",
                payload={"coins": [{"id": "wif"}]},
            )
            m.get(
                f"{CG_DEMO}/coins/markets?vs_currency=usd&ids=wif&per_page=10"
                "&x_cg_demo_api_key=CG-demokey",
                payload=[
                    {
                        "id": "wif",
                        "symbol": "wif",
                        "market_cap": 5_000_000_000.0,
                        "current_price": 0.5,
                        "total_volume": 100_000.0,
                    }
                ],
            )
            result = await resolve_and_enrich(
                [], ["WIF"], session=session, settings=s, is_retry=False
            )
    assert result.state == ResolutionState.RESOLVED
    assert [c.token_id for c in result.candidates_top3] == ["wif"]
    assert all("x_cg_pro_api_key" not in u for u in _requested_urls(m))


@pytest.mark.asyncio
async def test_empty_cg_key_sends_no_auth_param(settings_factory):
    """No key configured → no auth param on the wire, resolution still works."""
    s = settings_factory()
    assert s.COINGECKO_API_KEY == ""
    ref = ContractRef(chain="ethereum", address="0xAbc")
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                _cg_contract_url("ethereum", "0xAbc"),
                payload={"id": "test-coin", "symbol": "TST", "market_data": {}},
            )
            result = await resolve_and_enrich(
                [ref], [], session=session, settings=s, is_retry=False
            )
    assert result.state == ResolutionState.RESOLVED
    assert all("api_key" not in u for u in _requested_urls(m))


@pytest.mark.asyncio
async def test_dexscreener_stays_keyless_and_ungated(monkeypatch, settings_factory):
    """DexScreener is a different provider: no CG key, no CG limiter gating,
    even when a pro CG key is configured."""
    s = settings_factory(COINGECKO_API_KEY="CG-prokey", COINGECKO_API_TIER="pro")
    calls = _install_fake_limiter(monkeypatch)
    ref = ContractRef(chain="ethereum", address="0xAbc")
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                f"{_cg_contract_url('ethereum', '0xAbc', CG_PRO)}"
                "?x_cg_pro_api_key=CG-prokey",
                status=404,
            )
            m.get(
                f"{DEXSCREENER_BASE}/0xAbc",
                payload={
                    "pairs": [
                        {
                            "chainId": "base",
                            "baseToken": {"symbol": "DEXONLY"},
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
    assert result.tokens[0].symbol == "DEXONLY"
    dex_urls = [u for u in _requested_urls(m) if u.startswith(DEXSCREENER_BASE)]
    assert dex_urls
    assert all("api_key" not in u for u in dex_urls)
    # Only the single CG call acquired the limiter; DexScreener did not.
    assert calls["acquire"] == 1


@pytest.mark.asyncio
async def test_pro_host_still_gated_by_shared_limiter(monkeypatch, settings_factory):
    """The limiter gate follows the RESOLVED base — a stale demo-host constant
    would leave pro-tier calls ungated and their 429s unreported."""
    s = settings_factory(COINGECKO_API_KEY="CG-prokey", COINGECKO_API_TIER="pro")
    calls = _install_fake_limiter(monkeypatch)

    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                f"{CG_PRO}/search?query=WIF&x_cg_pro_api_key=CG-prokey",
                status=429,
            )
            outcome, data = await _get_json(
                session, f"{_cg_base(s)}/search", s, params={"query": "WIF"}
            )

    assert outcome == _Outcome.TRANSIENT
    assert data is None
    assert calls == {"acquire": 1, "report_429": 1}
