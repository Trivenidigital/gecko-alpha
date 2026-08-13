"""W2 — durable resolution snapshot: canonical form, null-vs-absent, round-trip.

The snapshot is the ONLY permitted recovery source for a historical shadow
decision, so its serialization has to be byte-stable across processes: a
snapshot that re-serializes differently would split a cohort on nothing.
"""

from __future__ import annotations

import json

import pytest

from scout.social.telegram.models import ResolvedToken
from scout.social.telegram.snapshot import (
    SNAPSHOT_PRODUCER_SEMANTIC_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    build_resolution_snapshot,
    canonical_json_dumps,
    module_source_hash,
    parse_resolution_snapshot,
)


def _token(**overrides) -> ResolvedToken:
    defaults = dict(
        token_id="tok",
        symbol="TOK",
        chain="solana",
        contract_address="0xabc",
        mcap=250_000.0,
        price_usd=0.00031415,
        volume_24h_usd=42_000.5,
        liquidity_usd=48_250.75,
        age_days=3.25,
        safety_pass=True,
        safety_check_completed=True,
        safety_skipped_no_ca=False,
    )
    defaults.update(overrides)
    return ResolvedToken(**defaults)


def test_snapshot_round_trips_every_field():
    text = build_resolution_snapshot(_token())
    parsed = parse_resolution_snapshot(text)
    assert parsed == {
        "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION,
        "price_usd": 0.00031415,
        "volume_24h_usd": 42_000.5,
        "age_days": 3.25,
        "liquidity_usd": 48_250.75,
        "safety_pass": True,
        "safety_check_completed": True,
        "safety_skipped_no_ca": False,
    }


def test_liquidity_is_carried_when_the_source_supplied_it():
    """DexScreener-resolved tokens carry a real value (the resolver already
    reads `liquidity.usd` to pick the deepest pair). See
    tests/test_tg_shadow_liquidity.py for the full resolver→evaluator chain."""
    parsed = parse_resolution_snapshot(build_resolution_snapshot(_token()))
    assert parsed["liquidity_usd"] == 48_250.75


def test_liquidity_is_null_but_present_when_the_source_had_none():
    """CoinGecko-resolved tokens have no equivalent field. The key is still
    emitted: null means "this source could not supply it", whereas an absent
    key would mean "this schema version predates the field"."""
    parsed = parse_resolution_snapshot(
        build_resolution_snapshot(_token(liquidity_usd=None))
    )
    assert "liquidity_usd" in parsed
    assert parsed["liquidity_usd"] is None


def test_unavailable_value_is_null_not_absent():
    """null means 'the resolver could not supply it'; an ABSENT key would mean
    'this schema version predates the field'. The two must stay distinct."""
    parsed = parse_resolution_snapshot(
        build_resolution_snapshot(_token(price_usd=None, volume_24h_usd=None))
    )
    assert parsed["price_usd"] is None
    assert parsed["volume_24h_usd"] is None
    assert set(parsed) == {
        "snapshot_schema_version",
        "price_usd",
        "volume_24h_usd",
        "age_days",
        "liquidity_usd",
        "safety_pass",
        "safety_check_completed",
        "safety_skipped_no_ca",
    }


def test_two_builds_are_byte_identical():
    token = _token()
    assert build_resolution_snapshot(token) == build_resolution_snapshot(token)


def test_canonical_form_is_key_order_independent():
    """Same mapping, different insertion order, identical bytes."""
    a = canonical_json_dumps({"b": 1, "a": 2.5, "c": None})
    b = canonical_json_dumps({"c": None, "a": 2.5, "b": 1})
    assert a == b
    assert a == '{"a":2.5,"b":1,"c":null}'


def test_canonical_form_uses_float_repr_without_normalization():
    """Floats serialize through repr — the shortest round-tripping form. A
    Decimal-normalizing encoder reads ambient context and can collide."""
    text = canonical_json_dumps({"x": 0.1 + 0.2})
    assert text == '{"x":0.30000000000000004}'
    assert json.loads(text)["x"] == 0.1 + 0.2


def test_canonical_form_rejects_non_finite():
    """NaN/Infinity are not JSON. Emitting them would produce a document that
    only Python can read back, silently breaking any other consumer."""
    with pytest.raises(ValueError):
        canonical_json_dumps({"x": float("nan")})


@pytest.mark.parametrize(
    "text",
    [None, "", "   ", "not json", "[1, 2, 3]", '"a string"', "null"],
)
def test_parse_returns_none_on_unusable_input(text):
    """Feeds `shadow_block_snapshot_missing` — never an exception, never a
    silent skip."""
    assert parse_resolution_snapshot(text) is None


def test_parse_accepts_a_json_object():
    assert parse_resolution_snapshot('{"snapshot_schema_version": 1}') == {
        "snapshot_schema_version": 1
    }


def test_module_source_hash_is_stable_sha256():
    first = module_source_hash()
    assert first == module_source_hash()
    assert len(first) == 64
    assert set(first) <= set("0123456789abcdef")


def test_declared_versions():
    assert SNAPSHOT_SCHEMA_VERSION == 1
    assert SNAPSHOT_PRODUCER_SEMANTIC_VERSION == "tg-snap-v1"
