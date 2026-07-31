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
    get_nonce_source,
    key_fingerprint,
    reset_nonce_sources_for_tests,
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


def test_secret_with_surrounding_whitespace_signs_identically():
    """A secret pasted into .env commonly carries a trailing newline.
    validate=True would reject it as malformed, so we strip first — the
    signature must be byte-identical to the clean secret's."""
    clean = sign_kraken_request(_DOC_URI_PATH, _DOC_POST_DATA, _DOC_NONCE, _DOC_SECRET)
    for dirty in (f"{_DOC_SECRET}\n", f"  {_DOC_SECRET}", f"\t{_DOC_SECRET}\r\n"):
        assert (
            sign_kraken_request(_DOC_URI_PATH, _DOC_POST_DATA, _DOC_NONCE, dirty)
            == clean
        )
    assert clean == _DOC_EXPECTED_SIGN


def test_genuinely_malformed_secret_still_raises_after_strip():
    """Stripping must not soften the validation — only whitespace is forgiven."""
    with pytest.raises(KrakenSigningError):
        sign_kraken_request(
            _DOC_URI_PATH, _DOC_POST_DATA, _DOC_NONCE, "  not@base64!  "
        )


# ---------- process-wide nonce registry ----------


def test_get_nonce_source_returns_same_instance_per_fingerprint():
    reset_nonce_sources_for_tests()
    assert get_nonce_source("fp-a") is get_nonce_source("fp-a")
    assert get_nonce_source("fp-a") is not get_nonce_source("fp-b")


@pytest.mark.asyncio
async def test_shared_source_is_monotonic_across_independent_holders():
    """Two callers that each looked the source up separately still draw from
    one counter — this is what stops two adapters colliding on one API key."""
    reset_nonce_sources_for_tests()
    fp = key_fingerprint("shared-key")
    a, b = get_nonce_source(fp), get_nonce_source(fp)
    values = []
    for _ in range(50):
        values.append(int(await a.next()))
        values.append(int(await b.next()))
    assert len(set(values)) == len(values)
    assert values == sorted(values)


@pytest.mark.asyncio
async def test_separate_instances_would_collide_regression_guard():
    """Demonstrates the defect the registry exists to prevent: two
    INDEPENDENT KrakenNonce objects seed from the same wall clock and emit
    duplicates. If this ever stops colliding the registry is still correct,
    but the shared-source tests above are what actually protect the key."""
    a, b = KrakenNonce(), KrakenNonce()
    values = [int(await a.next()) for _ in range(25)]
    values += [int(await b.next()) for _ in range(25)]
    assert len(set(values)) < len(values), "expected independent counters to collide"
