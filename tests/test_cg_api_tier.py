"""COINGECKO_API_TIER switch (Pro-key support).

Paid CG plans (Basic/Analyst/Lite) authenticate against a DIFFERENT host
(pro-api.coingecko.com) with a DIFFERENT auth name (x-cg-pro-api-key /
x_cg_pro_api_key). A Pro key against the Demo host returns error 10010 and
vice versa (10011), so tier must be a single explicit setting — never
inferred from the key (both formats start with "CG-").

Covers: the scout.cg_api helper (single source of truth), the Settings field,
and wire-level behavior of representative call sites under tier="pro".
"""

import pytest
from aioresponses import aioresponses

import aiohttp

from scout import cg_api

# ---------------------------------------------------------------- cg_api unit


def test_base_url_demo():
    assert cg_api.base_url("demo") == "https://api.coingecko.com/api/v3"


def test_base_url_pro():
    assert cg_api.base_url("pro") == "https://pro-api.coingecko.com/api/v3"


def test_base_url_invalid_tier_raises():
    with pytest.raises(ValueError):
        cg_api.base_url("premium")


def test_auth_query_demo_and_pro():
    assert cg_api.auth_query("CG-k", "demo") == {"x_cg_demo_api_key": "CG-k"}
    assert cg_api.auth_query("CG-k", "pro") == {"x_cg_pro_api_key": "CG-k"}


def test_auth_headers_demo_and_pro():
    assert cg_api.auth_headers("CG-k", "demo") == {"x-cg-demo-api-key": "CG-k"}
    assert cg_api.auth_headers("CG-k", "pro") == {"x-cg-pro-api-key": "CG-k"}


def test_auth_empty_key_yields_empty_mapping():
    assert cg_api.auth_query("", "demo") == {}
    assert cg_api.auth_headers("", "pro") == {}


def test_auth_invalid_tier_raises():
    with pytest.raises(ValueError):
        cg_api.auth_query("CG-k", "basic")
    with pytest.raises(ValueError):
        cg_api.auth_headers("CG-k", "basic")


# ---------------------------------------------------------------- Settings


def test_settings_tier_defaults_to_demo(settings_factory):
    s = settings_factory()
    assert s.COINGECKO_API_TIER == "demo"


def test_settings_tier_accepts_pro(settings_factory):
    s = settings_factory(COINGECKO_API_TIER="pro")
    assert s.COINGECKO_API_TIER == "pro"


def test_settings_tier_rejects_unknown(settings_factory):
    with pytest.raises(Exception):
        settings_factory(COINGECKO_API_TIER="premium")


# ------------------------------------------------- wire-level: ingestion lane


@pytest.mark.asyncio
async def test_fetch_trending_uses_pro_host_and_param(settings_factory):
    """With tier=pro the trending lane must hit pro-api.coingecko.com and
    authenticate via x_cg_pro_api_key (a Demo-host call would 10010)."""
    from scout.ingestion import coingecko as cg

    settings = settings_factory(COINGECKO_API_KEY="CG-prokey", COINGECKO_API_TIER="pro")
    with aioresponses() as m:
        m.get(
            "https://pro-api.coingecko.com/api/v3/search/trending"
            "?x_cg_pro_api_key=CG-prokey",
            payload={"coins": []},
        )
        async with aiohttp.ClientSession() as session:
            result = await cg.fetch_trending(session, settings)
    assert result == []


@pytest.mark.asyncio
async def test_fetch_trending_demo_default_unchanged(settings_factory):
    """Regression pin: default tier stays demo — existing behavior intact."""
    from scout.ingestion import coingecko as cg

    settings = settings_factory(COINGECKO_API_KEY="CG-demokey")
    with aioresponses() as m:
        m.get(
            "https://api.coingecko.com/api/v3/search/trending"
            "?x_cg_demo_api_key=CG-demokey",
            payload={"coins": []},
        )
        async with aiohttp.ClientSession() as session:
            result = await cg.fetch_trending(session, settings)
    assert result == []


# ------------------------------------------- wire-level: api_key-only helper


@pytest.mark.asyncio
async def test_tracker_fetch_trending_pro_tier(tmp_path):
    """fetch_and_store_trending gains api_tier and must hit the pro host."""
    from scout.db import Database
    from scout.trending.tracker import fetch_and_store_trending

    db = Database(tmp_path / "t.db")
    await db.initialize()
    with aioresponses() as m:
        m.get(
            "https://pro-api.coingecko.com/api/v3/search/trending"
            "?x_cg_pro_api_key=CG-prokey",
            payload={"coins": []},
        )
        async with aiohttp.ClientSession() as session:
            snaps = await fetch_and_store_trending(
                session, db, api_key="CG-prokey", api_tier="pro"
            )
    assert snaps == []
    await db.close()
