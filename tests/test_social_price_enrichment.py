"""Tests for scout.social.lunarcrush.price -- enrichment from raw markets."""

from __future__ import annotations

import pytest

from scout.social.lunarcrush.price import get_price_change_1h


def test_match_by_lower_symbol(monkeypatch):
    """A token with matching LOWER(symbol) returns both 1h and 24h deltas."""
    from scout.ingestion import coingecko as cg

    monkeypatch.setattr(
        cg,
        "last_raw_markets",
        [
            {
                "id": "asteroid-shiba",
                "symbol": "ast",
                "price_change_percentage_1h_in_currency": 42.1,
                "price_change_percentage_24h": 114_775.8,
            }
        ],
    )
    ch_1h, ch_24h, cg_slug = get_price_change_1h("AST", coin_id=None)
    assert ch_1h == 42.1
    assert ch_24h == 114_775.8
    assert cg_slug == "asteroid-shiba"


def test_match_by_coin_id(monkeypatch):
    from scout.ingestion import coingecko as cg

    monkeypatch.setattr(
        cg,
        "last_raw_markets",
        [
            {
                "id": "asteroid-shiba",
                "symbol": "ast",
                "price_change_percentage_1h_in_currency": 10.0,
                "price_change_percentage_24h": 50.0,
            }
        ],
    )
    ch_1h, ch_24h, cg_slug = get_price_change_1h(None, coin_id="asteroid-shiba")
    assert ch_1h == 10.0
    assert ch_24h == 50.0
    assert cg_slug == "asteroid-shiba"


def test_no_match_returns_none_none(monkeypatch):
    from scout.ingestion import coingecko as cg

    monkeypatch.setattr(cg, "last_raw_markets", [])
    assert get_price_change_1h("NOPE", coin_id="nope") == (None, None, None)


def test_never_raises_on_malformed(monkeypatch):
    """A malformed entry in last_raw_markets does not raise.

    A truly-absent token returns (None, None, None). A coin_id match on an
    otherwise-empty entry still returns the slug (callers are explicitly
    designed to handle a slug-only result).
    """
    from scout.ingestion import coingecko as cg

    monkeypatch.setattr(
        cg,
        "last_raw_markets",
        [
            {},  # empty
            "not-a-dict",  # wrong type
            {"id": "foo", "symbol": None},  # missing fields
        ],
    )
    # No match anywhere -- no price, no slug.
    assert get_price_change_1h("NOPE", coin_id="nope") == (None, None, None)
    # id match but no price fields -- slug is still returned so the alerter
    # can render a working CG link even when deltas are missing.
    assert get_price_change_1h("FOO", coin_id="foo") == (None, None, "foo")
