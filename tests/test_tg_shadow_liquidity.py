"""Liquidity plumbing: resolver → ResolvedToken → snapshot → evaluator.

Operator requirement. `liquidity_usd` was previously emitted as a global
`null`, which would have made `shadow_block_liquidity_unknown` a property of
the SCHEMA rather than of the data — and if the liquidity check were ever
turned on, the entire population would collapse into that one reason for a
reason nobody chose. The DexScreener response already carries liquidity (the
resolver reads `liquidity.usd` to pick the deepest pair, then discarded it), so
the value is real for that source and honestly null for CoinGecko.

These tests pin both halves of that claim:
  * the real value survives the whole chain for a DexScreener-resolved token;
  * missingness is intentional and threshold-bound — with the default
    `TG_SHADOW_REQUIRE_LIQUIDITY=False` a null-liquidity snapshot can still
    reach `shadow_pass`, and with it True the SAME fixture deterministically
    blocks.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import aiohttp
import pytest
from aioresponses import aioresponses

from scout import cg_api
from scout.safety import GOPLUS_BASE
from scout.social.telegram.models import ContractRef, ResolutionState, ResolvedToken
from scout.social.telegram.resolver import DEXSCREENER_BASE, resolve_and_enrich
from scout.social.telegram.shadow import evaluate_tg_actionability_shadow
from scout.social.telegram.snapshot import (
    build_resolution_snapshot,
    parse_resolution_snapshot,
)

_ADDR = "0xAbc"
_LIQUIDITY = 48_250.75


def _signal_row(**overrides) -> dict:
    defaults = dict(
        id=1,
        token_id="dex:base:0xAbc",
        symbol="DEXMEME",
        contract_address=_ADDR,
        chain="base",
        mcap_at_sighting=250_000.0,
        resolution_state="RESOLVED",
        source_channel_handle="@gem",
        created_at=datetime(2026, 8, 12, tzinfo=timezone.utc).isoformat(),
    )
    defaults.update(overrides)
    return defaults


def _features() -> dict:
    return {
        "history_eligible_distinct_clusters": 25,
        "history_priceable_coverage_rate": 0.9,
        "event_duplicate_rank_in_cluster": 1,
    }


def _dex_payload(liquidity: float | None = _LIQUIDITY) -> dict:
    pair: dict = {
        "chainId": "base",
        "baseToken": {"symbol": "DEXMEME"},
        "fdv": 250_000.0,
        "priceUsd": "0.0023",
        "volume": {"h24": 5000.0},
    }
    if liquidity is not None:
        pair["liquidity"] = {"usd": liquidity}
    return {"pairs": [pair]}


async def _resolve_via_dexscreener(settings, payload: dict) -> ResolvedToken:
    """Public resolver entry, CG missing so the DexScreener path is taken.

    GoPlus is mocked to a clean verdict so the safety trio does not shadow the
    thing under test; the liquidity assertions are what this exercises.
    """
    ref = ContractRef(chain="base", address=_ADDR)
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                f"{cg_api.DEMO_BASE}/coins/base/contract/{_ADDR}",
                status=404,
            )
            m.get(f"{DEXSCREENER_BASE}/{_ADDR}", payload=payload)
            # aioresponses matches the query string too; is_safe_strict
            # sends ?contract_addresses=<addr>.
            m.get(
                f"{GOPLUS_BASE}/8453?contract_addresses={_ADDR}",
                payload={
                    "code": 1,
                    "result": {
                        _ADDR.lower(): {
                            "is_honeypot": "0",
                            "is_blacklisted": "0",
                            "buy_tax": "0",
                            "sell_tax": "0",
                        }
                    },
                },
            )
            result = await resolve_and_enrich(
                [ref], [], session=session, settings=settings, is_retry=False
            )
    assert result.state == ResolutionState.RESOLVED, result.error_text
    return result.tokens[0]


# ---------------------------------------------------------------------------
# resolver → ResolvedToken
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_forwards_dexscreener_liquidity(settings_factory):
    """The value the pair-selection already read is no longer discarded."""
    token = await _resolve_via_dexscreener(settings_factory(), _dex_payload())
    assert token.liquidity_usd == pytest.approx(_LIQUIDITY)


@pytest.mark.asyncio
async def test_resolver_picks_the_deepest_pair_and_reports_its_liquidity(
    settings_factory,
):
    """Liquidity and pair choice must come from the SAME pair — reporting one
    pair's liquidity for another pair's price would be a quiet lie."""
    payload = {
        "pairs": [
            {
                "chainId": "base",
                "baseToken": {"symbol": "DEXMEME"},
                "fdv": 250_000.0,
                "priceUsd": "0.001",
                "volume": {"h24": 100.0},
                "liquidity": {"usd": 1_000.0},
            },
            {
                "chainId": "base",
                "baseToken": {"symbol": "DEXMEME"},
                "fdv": 250_000.0,
                "priceUsd": "0.0023",
                "volume": {"h24": 5000.0},
                "liquidity": {"usd": _LIQUIDITY},
            },
        ]
    }
    token = await _resolve_via_dexscreener(settings_factory(), payload)
    assert token.liquidity_usd == pytest.approx(_LIQUIDITY)
    assert token.price_usd == pytest.approx(0.0023)


@pytest.mark.asyncio
async def test_null_liquidity_pair_does_not_crash_the_resolver(settings_factory):
    """DexScreener sends `"liquidity": null` for some pairs. The pair-selection
    key used to do `.get("liquidity", {}).get("usd")`, which returns that None
    and raises AttributeError — losing the whole resolution, not just the
    liquidity."""
    payload = {
        "pairs": [
            {
                "chainId": "base",
                "baseToken": {"symbol": "DEXMEME"},
                "fdv": 250_000.0,
                "priceUsd": "0.0023",
                "volume": {"h24": 5000.0},
                "liquidity": None,
            }
        ]
    }
    token = await _resolve_via_dexscreener(settings_factory(), payload)
    assert token.liquidity_usd is None
    assert token.symbol == "DEXMEME"


@pytest.mark.asyncio
async def test_coingecko_resolution_reports_null_liquidity(settings_factory):
    """Honest per-source availability: the CG by-contract response has no
    liquidity field, so null here means "this source cannot supply it"."""
    ref = ContractRef(chain="ethereum", address=_ADDR)
    async with aiohttp.ClientSession() as session:
        with aioresponses() as m:
            m.get(
                f"{cg_api.DEMO_BASE}/coins/ethereum/contract/{_ADDR}",
                payload={
                    "id": "test-coin",
                    "symbol": "TST",
                    "market_data": {
                        "market_cap": {"usd": 5_000_000.0},
                        "current_price": {"usd": 1.23},
                        "total_volume": {"usd": 100_000.0},
                    },
                },
            )
            m.get(
                f"{GOPLUS_BASE}/1?contract_addresses={_ADDR}",
                payload={
                    "code": 1,
                    "result": {_ADDR.lower(): {"is_honeypot": "0", "buy_tax": "0"}},
                },
            )
            result = await resolve_and_enrich(
                [ref], [], session=session, settings=settings_factory(), is_retry=False
            )
    assert result.tokens[0].liquidity_usd is None


# ---------------------------------------------------------------------------
# ResolvedToken → snapshot
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_liquidity_survives_the_snapshot_round_trip(settings_factory):
    token = await _resolve_via_dexscreener(settings_factory(), _dex_payload())
    snapshot = parse_resolution_snapshot(build_resolution_snapshot(token))
    assert snapshot["liquidity_usd"] == pytest.approx(_LIQUIDITY)
    # And it is a real JSON number, not a stringified float.
    raw = json.loads(build_resolution_snapshot(token))
    assert isinstance(raw["liquidity_usd"], float)


def test_snapshot_records_null_when_the_source_had_none():
    token = ResolvedToken(token_id="cg-coin", symbol="CG", liquidity_usd=None)
    snapshot = parse_resolution_snapshot(build_resolution_snapshot(token))
    assert "liquidity_usd" in snapshot
    assert snapshot["liquidity_usd"] is None


# ---------------------------------------------------------------------------
# snapshot → evaluator, both sides of the configuration contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_real_liquidity_reaches_the_evaluator_and_passes_when_required(
    settings_factory,
):
    """End of the chain: a DexScreener-resolved token clears the liquidity
    check even with `TG_SHADOW_REQUIRE_LIQUIDITY=True`, which is only possible
    because the real value made it all the way through."""
    token = await _resolve_via_dexscreener(settings_factory(), _dex_payload())
    snapshot = parse_resolution_snapshot(build_resolution_snapshot(token))
    assert snapshot["liquidity_usd"] == pytest.approx(_LIQUIDITY)

    decision = evaluate_tg_actionability_shadow(
        _signal_row(),
        snapshot,
        _features(),
        settings_factory(TG_SHADOW_REQUIRE_LIQUIDITY=True),
    )
    assert decision.reason == "shadow_pass"
    assert decision.actionable is True


def _null_liquidity_snapshot() -> dict:
    """One fixture, used by BOTH configuration-side tests below — the point is
    that the same data yields different decisions only because the setting
    changed."""
    return parse_resolution_snapshot(
        build_resolution_snapshot(
            ResolvedToken(
                token_id="cg-coin",
                symbol="CG",
                contract_address=_ADDR,
                chain="ethereum",
                price_usd=1.0,
                volume_24h_usd=1000.0,
                liquidity_usd=None,
                safety_pass=True,
                safety_check_completed=True,
            )
        )
    )


def test_null_liquidity_still_reaches_shadow_pass_by_default(settings_factory):
    """Default `TG_SHADOW_REQUIRE_LIQUIDITY=False`: a source that cannot supply
    liquidity must not degenerate the population into a single reason. This is
    the anti-degeneracy guarantee."""
    settings = settings_factory()
    assert settings.TG_SHADOW_REQUIRE_LIQUIDITY is False

    decision = evaluate_tg_actionability_shadow(
        _signal_row(), _null_liquidity_snapshot(), _features(), settings
    )
    assert decision.reason == "shadow_pass"
    assert decision.actionable is True


def test_same_null_liquidity_snapshot_blocks_when_required(settings_factory):
    """Same fixture, `TG_SHADOW_REQUIRE_LIQUIDITY=True`: deterministic block.

    Together with the test above this proves the setting is genuinely consumed
    by the evaluator rather than dead config, and that the block is a chosen
    policy rather than a schema artifact."""
    decision = evaluate_tg_actionability_shadow(
        _signal_row(),
        _null_liquidity_snapshot(),
        _features(),
        settings_factory(TG_SHADOW_REQUIRE_LIQUIDITY=True),
    )
    assert decision.reason == "shadow_block_liquidity_unknown"
    assert decision.actionable is False
