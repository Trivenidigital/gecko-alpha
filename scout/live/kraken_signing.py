"""Kraken Spot REST signing primitive + nonce management (PR-K1).

Pure module — no I/O, no aiohttp. Tested against Kraken's published worked
example independently of the adapter layer, mirroring
``scout/live/binance_signing.py``.

Verified against current Kraken docs on 2026-07-31
(https://docs.kraken.com/api/docs/guides/spot-rest-auth):

    API-Sign = base64(
        HMAC-SHA512(
            key = base64_decode(private_key),
            msg = URI_path_bytes + SHA256(nonce_str + POST_body_str),
        )
    )

- ``nonce`` is an always-increasing unsigned 64-bit integer and MUST also
  appear as a field inside the form-encoded POST body — it is hashed as the
  literal string prefix of that body, so the ``nonce=`` field has to be the
  same value and must be present in the body that is actually sent.
- Request headers are ``API-Key`` (public key, verbatim) and ``API-Sign``
  (the base64 string this module returns).
- The POST body is ``application/x-www-form-urlencoded``.

Kraken's published example (reproduced byte-for-byte in
``tests/test_live_kraken_signing.py``) uses URI path ``/0/private/AddOrder``,
nonce ``1616492376594`` and yields
``4/dpxb3iT4tp/ZCVEwSnEsLxx0bqyhLpdfOpc6fn7OR8+UClSV5n9E6aSS8MPtnRfp32bAb0nmbRn6H8ndwLUQ==``.

Nothing here logs — the secret, the signature and the nonce/postdata pair
never leave the call stack.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import threading
import time


class KrakenSigningError(Exception):
    """The configured API secret is not decodable base64.

    Never carries the secret in its message — only the fact that decoding
    failed, so the exception is safe to log.
    """


def sign_kraken_request(
    url_path: str,
    post_data: str,
    nonce: str,
    api_secret_b64: str,
) -> str:
    """Compute the Kraken ``API-Sign`` header value.

    Args:
        url_path: URI path INCLUDING the API version, e.g.
            ``/0/private/Balance``. Must not include the scheme/host or a
            query string — Kraken signs the path exactly as sent.
        post_data: The form-encoded request body exactly as it will be sent
            on the wire, e.g. ``nonce=1616492376594&asset=ZUSD``. Must
            already contain the ``nonce`` field with the same value passed
            as ``nonce``.
        nonce: The nonce as a decimal string. Hashed as a literal prefix of
            ``post_data`` — passing an int-formatted-differently value here
            than in the body produces a signature Kraken rejects.
        api_secret_b64: The operator's Kraken private key, base64 as issued
            by Kraken (NOT decoded).

    Returns:
        The base64-encoded HMAC-SHA512 digest for the ``API-Sign`` header.

    Raises:
        KrakenSigningError: ``api_secret_b64`` is not valid base64.
    """
    try:
        # .strip() first: a secret pasted into .env commonly carries a
        # trailing newline or space, which validate=True would reject as
        # malformed. Stripping means we only fail loud on a genuinely bad
        # secret, never on invisible whitespace.
        secret_bytes = base64.b64decode(api_secret_b64.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise KrakenSigningError(
            "KRAKEN_API_SECRET is not valid base64 — re-copy the private key "
            "from the Kraken API key management page"
        ) from exc

    sha256_digest = hashlib.sha256((nonce + post_data).encode("utf-8")).digest()
    message = url_path.encode("utf-8") + sha256_digest
    signature = hmac.new(secret_bytes, message, hashlib.sha512).digest()
    return base64.b64encode(signature).decode("utf-8")


class KrakenNonce:
    """Monotonic nonce source for Kraken private requests.

    Kraken rejects any nonce that is not strictly greater than the previous
    one used by the same key (``EAPI:Invalid nonce``), and a single wall-clock
    millisecond can easily serve several concurrent calls. This class hands
    out millisecond-based values but guarantees strict monotonicity by
    bumping past the last issued value whenever the clock has not advanced —
    including when the clock moves BACKWARDS (NTP step), which is the failure
    mode that otherwise locks a key out until real time catches up.

    Serialized by a ``threading.Lock``, NOT an ``asyncio.Lock``. Two reasons:
    an instance is shared process-wide across every adapter using the same
    API key (see ``get_nonce_source``), so it may legitimately be reached
    from more than one event loop — and an ``asyncio.Lock`` binds to the
    first loop that awaits it, which would raise on the second. The critical
    section is a clock read, a compare and an assignment with no ``await``
    inside it, so it cannot block the loop; on a single loop it is already
    atomic and the lock only matters for genuine cross-thread access, which
    is exactly what ``threading.Lock`` covers and ``asyncio.Lock`` does not.
    """

    def __init__(self, *, start: int | None = None) -> None:
        """Args:
        start: Seed for the last-issued value. Defaults to 0, i.e. the
            first nonce is the current wall-clock millisecond. Tests pass
            an explicit seed to exercise the clock-not-advanced path.
        """
        self._last: int = start if start is not None else 0
        self._lock = threading.Lock()

    async def next(self) -> str:
        """Return the next nonce as a decimal string.

        Strictly greater than every value previously returned by this
        instance, for any interleaving of concurrent callers.
        """
        with self._lock:
            candidate = int(time.time() * 1000)
            if candidate <= self._last:
                candidate = self._last + 1
            self._last = candidate
            return str(candidate)


# ----------------------------------------------------------------------
# Process-wide nonce registry.
#
# Kraken counts nonces PER API KEY, requires them ever-increasing, and
# cannot reset them. A per-adapter-instance counter therefore breaks the
# moment two adapters share a key: both seed from the same wall clock and
# emit colliding values, and the loser gets EAPI:Invalid nonce (or, after
# enough of them, EGeneral:Temporary lockout on the whole key).
#
# Keyed by a fingerprint of the API key rather than the key itself so the
# registry is safe to inspect and log.
# ----------------------------------------------------------------------
_NONCE_SOURCES: dict[str, KrakenNonce] = {}
_REGISTRY_LOCK = threading.Lock()


def key_fingerprint(api_key: str) -> str:
    """Stable, non-reversible id for an API key — safe to log.

    Truncated SHA-256. Only ever used to partition nonce counters, so
    collision resistance at 64 bits is far beyond what's needed.
    """
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def get_nonce_source(fingerprint: str) -> KrakenNonce:
    """Return THE process-wide nonce source for ``fingerprint``.

    Every adapter instance authenticating with the same API key draws from
    one monotonic counter, so nonces are globally increasing per key no
    matter how many adapters, engines or loops exist in the process.
    """
    with _REGISTRY_LOCK:
        source = _NONCE_SOURCES.get(fingerprint)
        if source is None:
            source = KrakenNonce()
            _NONCE_SOURCES[fingerprint] = source
        return source


def reset_nonce_sources_for_tests() -> None:
    """Drop every registered nonce source. Tests only.

    The registry is deliberately process-global, which would otherwise leak
    counter state between tests.
    """
    with _REGISTRY_LOCK:
        _NONCE_SOURCES.clear()
