"""BL-NEW-LIVE-HYBRID M1.5a: Binance HMAC-SHA256 signing primitive.

Pure function — no I/O, no aiohttp. Tested against Binance's published
HMAC fixture independently of the adapter layer.

Per Binance SIGNED endpoint security spec:
https://binance-docs.github.io/apidocs/spot/en/#signed-trade-and-user_data-endpoint-security

The signature is computed as HMAC-SHA256 of the query string (URL-encoded
key=value joined by &), keyed by the operator's secret. The result is
appended as `signature=<hex>` and sent either:
  - GET: as a final query parameter (this implementation's choice)
  - POST: as a final form field (we use query string for both — Binance accepts either)
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any
from urllib.parse import urlencode


def sign_request(
    secret: str, params: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """Sign a Binance request.

    Args:
        secret: The operator's BINANCE_API_SECRET (raw string, NOT URL-encoded).
        params: Request parameters in insertion order. Must already include
            `timestamp` and (recommended) `recvWindow`. Must NOT include
            `signature` — this function appends it.

    Returns:
        (signed_params, signature_hex) where:
        - signed_params is a NEW dict with the original params (insertion-
          order preserved) plus a final `signature` key.
        - signature_hex is the hex-encoded HMAC-SHA256 digest.

    Notes:
        - Insertion order matters because urlencode() is order-preserving
          on Python 3.7+ dict iteration. Caller is responsible for ordering.
        - Caller's dict is NOT mutated — function returns a fresh dict.
    """
    query_string = urlencode(params)
    digest = hmac.new(
        secret.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    signed_params = dict(params)
    signed_params["signature"] = digest
    return signed_params, digest
