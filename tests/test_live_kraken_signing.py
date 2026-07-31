"""PR-K1: Kraken HMAC-SHA512 signing primitive + nonce monotonicity tests.

Pure-stdlib (no aiohttp) — mirrors tests/test_live_binance_signing.py so the
signing contract stays covered even where the HTTP stack won't import.
"""

from __future__ import annotations

import asyncio

import pytest

from scout.live.kraken_signing import (
    KrakenNonce,
    KrakenSigningError,
    sign_kraken_request,
)

# Kraken's OFFICIAL worked example, verbatim from the Spot REST
# authentication guide (https://docs.kraken.com/api/docs/guides/spot-rest-auth,
# retrieved 2026-07-31). The private key below is Kraken's own published
# sample value, not a real credential.
_DOC_SECRET = (
    "kQH5HW/8p1uGOVjbgWA7FunAmGO8lsSUXNsu3eow76sz84Q18fWxnyRzBHCd3pd5"
    "nE9qa99HAZtuZuj6F1huXg=="
)
_DOC_NONCE = "1616492376594"
_DOC_POST_DATA = (
    "nonce=1616492376594&ordertype=limit&pair=XBTUSD"
    "&price=37500&type=buy&volume=1.25"
)
_DOC_URI_PATH = "/0/private/AddOrder"
_DOC_EXPECTED_SIGN = (
    "4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp"
    "32bAb0nmbRn6H8ndwLUQ=="
)


def test_sign_kraken_request_matches_official_doc_vector():
    """The whole auth scheme rides on this one vector reproducing exactly."""
    signature = sign_kraken_request(
        _DOC_URI_PATH, _DOC_POST_DATA, _DOC_NONCE, _DOC_SECRET
    )
    assert signature == _DOC_EXPECTED_SIGN


def test_sign_kraken_request_depends_on_uri_path():
    """The URI path is part of the signed message — a different endpoint
    with identical body must not produce a reusable signature."""
    other = sign_kraken_request(
        "/0/private/Balance", _DOC_POST_DATA, _DOC_NONCE, _DOC_SECRET
    )
    assert other != _DOC_EXPECTED_SIGN


def test_sign_kraken_request_depends_on_nonce():
    """The nonce is hashed as a prefix of the post data — changing it (but
    not the body) must change the signature."""
    other = sign_kraken_request(
        _DOC_URI_PATH, _DOC_POST_DATA, "1616492376595", _DOC_SECRET
    )
    assert other != _DOC_EXPECTED_SIGN


def test_sign_kraken_request_depends_on_secret():
    other = sign_kraken_request(
        _DOC_URI_PATH,
        _DOC_POST_DATA,
        _DOC_NONCE,
        "bm90LXRoZS1yZWFsLXNlY3JldC1hdC1hbGw=",
    )
    assert other != _DOC_EXPECTED_SIGN


def test_sign_kraken_request_rejects_non_base64_secret():
    """A pasted-wrong secret must fail loudly at signing time, and the
    message must not echo the secret."""
    bad = "not-base64-!!!"
    with pytest.raises(KrakenSigningError) as excinfo:
        sign_kraken_request(_DOC_URI_PATH, _DOC_POST_DATA, _DOC_NONCE, bad)
    assert bad not in str(excinfo.value)


# ---------- KrakenNonce ----------


@pytest.mark.asyncio
async def test_nonce_is_strictly_increasing_across_rapid_calls():
    """Successive calls inside the same millisecond must still increase —
    Kraken rejects a repeated nonce with EAPI:Invalid nonce."""
    nonce = KrakenNonce()
    values = [int(await nonce.next()) for _ in range(200)]
    assert all(b > a for a, b in zip(values, values[1:]))


@pytest.mark.asyncio
async def test_nonce_bumps_past_seed_when_clock_has_not_advanced():
    """Seeded far in the future (the shape of an NTP step backwards): the
    counter must keep climbing rather than reissue a stale value."""
    future_ms = 4_102_444_800_000  # 2100-01-01, well beyond wall clock
    nonce = KrakenNonce(start=future_ms)
    first = int(await nonce.next())
    second = int(await nonce.next())
    assert first == future_ms + 1
    assert second == first + 1


@pytest.mark.asyncio
async def test_nonce_unique_under_concurrent_callers():
    """Concurrent coroutines must never observe the same counter value."""
    nonce = KrakenNonce()
    values = await asyncio.gather(*(nonce.next() for _ in range(100)))
    assert len(set(values)) == 100


@pytest.mark.asyncio
async def test_nonce_returns_decimal_strings():
    """The nonce goes into the form body verbatim and is hashed as a string
    — it must be a plain decimal, no separators or exponent form."""
    value = await KrakenNonce().next()
    assert value.isdigit()
    assert int(value) > 0
