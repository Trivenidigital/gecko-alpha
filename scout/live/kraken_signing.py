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

import asyncio
import base64
import binascii
import hashlib
import hmac
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
        secret_bytes = base64.b64decode(api_secret_b64, validate=True)
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

    Serialized by an ``asyncio.Lock`` so concurrent coroutines can never
    observe the same counter value.
    """

    def __init__(self, *, start: int | None = None) -> None:
        """Args:
        start: Seed for the last-issued value. Defaults to 0, i.e. the
            first nonce is the current wall-clock millisecond. Tests pass
            an explicit seed to exercise the clock-not-advanced path.
        """
        self._last: int = start if start is not None else 0
        self._lock = asyncio.Lock()

    async def next(self) -> str:
        """Return the next nonce as a decimal string.

        Strictly greater than every value previously returned by this
        instance, for any interleaving of concurrent callers.
        """
        async with self._lock:
            candidate = int(time.time() * 1000)
            if candidate <= self._last:
                candidate = self._last + 1
            self._last = candidate
            return str(candidate)
