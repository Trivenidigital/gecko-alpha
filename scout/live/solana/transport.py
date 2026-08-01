"""Shared async HTTP for the Solana lane's three upstreams (PR-S1).

Jupiter, the Solana RPC and the Jito block engine all need the same
error taxonomy, so it lives here once rather than three times. The taxonomy is
the Kraken lane's (``kraken_adapter._request``): the split that matters is
DEFINITIVE vs AMBIGUOUS, because the submit path treats a definitive error as
proof the transaction was refused and everything else as "it may exist".

The one genuine difference between the upstreams is what an HTTP 4xx MEANS,
and it is not cosmetic:

- **Jupiter is REST.** A 400 is the API deciding — bad mint, no route,
  unroutable amount. It carries an ``error`` body and is DEFINITIVE.
- **Jito and the Solana RPC are JSON-RPC.** Their refusals arrive as HTTP 200
  with an ``error`` member, exactly like Kraken's HTTP-200 error envelope. A
  raw 4xx from either therefore did NOT come from the block engine or the
  validator deciding anything — it is a proxy, an edge node or a load
  balancer, and it is fully compatible with the request having been relayed
  onward. Treating it as a refusal is precisely the reasoning that produces a
  duplicate submission.

Hence ``four_xx_is_definitive``, which callers must set deliberately.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import aiohttp
import structlog

from scout.live.exceptions import VenueTransientError
from scout.live.solana.exceptions import (
    SolanaAPIError,
    SolanaRateLimitError,
    SolanaResponseError,
)

log = structlog.get_logger(__name__)

# Same shape as the Kraken lane's ladder. Only ever walked when the caller
# opted into retries — the submit path never does.
_BACKOFFS: tuple[float, ...] = (0.5, 1.0, 2.0)

# Indirection so a test can walk the ladder without waiting for it. Mirrors
# `KrakenSpotAdapter._retry_sleep`, which exists for the same reason.
#
# Patch THIS, never `_BACKOFFS`: the schedule's LENGTH decides how many
# attempts a transient failure gets, so shortening the tuple would quietly
# change the retry semantics the tests are there to pin. Replacing the sleep
# leaves attempt counts, ordering and exhaustion behaviour exactly as they are
# in production and removes only the wall-clock.
_retry_sleep = asyncio.sleep

# Upstream bodies are echoed into exception messages so an operator can see
# WHY a build was refused, but truncated: a Jupiter error can carry a full
# route dump, and an unbounded string ends up in logs and evidence files.
_MAX_BODY_CHARS = 500


async def request_json(
    session: aiohttp.ClientSession,
    method: Literal["GET", "POST"],
    url: str,
    *,
    label: str,
    timeout_sec: float,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    retry_transient: bool = True,
    four_xx_is_definitive: bool,
    capture_headers: dict[str, str] | None = None,
) -> Any:
    """Issue one JSON request and classify every failure mode.

    Args:
        label: Short upstream name used in log events and error messages
            (``jupiter_quote``, ``jito_submit``, ...). Never contains a
            credential.
        retry_transient: Retry network errors and 5xx. Submission paths MUST
            pass ``False`` — a retried non-idempotent POST is the double-send
            risk this whole module is shaped around.
        four_xx_is_definitive: See the module docstring. ``True`` only for
            REST upstreams whose 4xx is the API's own decision.
        capture_headers: when given, populated with the response headers.
            Jito returns the bundle id in ``x-bundle-id`` rather than in the
            body, and that is the only handle on a submission's status.

    Raises:
        SolanaRateLimitError: HTTP 429. Never retried in-loop.
        SolanaAPIError: a definitive refusal (4xx when the caller declared
            4xx definitive, or a JSON-RPC ``error`` member surfaced by the
            caller).
        SolanaResponseError: HTTP 200 whose body is not JSON at all.
        VenueTransientError: network error, timeout, 5xx, HTML interstitial,
            or a 4xx from a JSON-RPC upstream. AMBIGUOUS — proves nothing
            about whether the request was acted on.
    """
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    last_exc: Exception | None = None

    for attempt in range(len(_BACKOFFS) + 1):
        try:
            # allow_redirects=False mirrors the Kraken signed-POST branch:
            # aiohttp re-sends method AND body on a 307/308, which on the
            # submit path is a second transmission inside a single attempt,
            # invisible to retry_transient=False.
            cm = session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )
            async with cm as resp:
                if capture_headers is not None:
                    # Captured before any raise: a 4xx or 5xx can still carry
                    # the bundle id, and on the submit path that header is the
                    # difference between a resolvable submission and a blind
                    # one.
                    capture_headers.update({k: v for k, v in resp.headers.items()})

                if resp.status == 429:
                    raise SolanaRateLimitError(f"{label}: HTTP 429")

                if 300 <= resp.status < 400:
                    # Never followed. A redirect is not a refusal, and
                    # whatever produced it may have relayed the body onward.
                    raise VenueTransientError(
                        f"{label}: unexpected redirect (HTTP {resp.status})"
                    )

                if 500 <= resp.status < 600:
                    last_exc = VenueTransientError(
                        f"{label}: HTTP {resp.status} attempt={attempt + 1}"
                    )
                    if retry_transient and attempt < len(_BACKOFFS):
                        await _retry_sleep(_BACKOFFS[attempt])
                        continue
                    raise last_exc

                if resp.status >= 400:
                    body = (await resp.text())[:_MAX_BODY_CHARS]
                    if four_xx_is_definitive:
                        raise SolanaAPIError(f"{label}: HTTP {resp.status}: {body}")
                    # JSON-RPC upstream — see module docstring. Raised
                    # immediately, never retried: re-sending a
                    # non-idempotent POST at something answering 4xx is the
                    # double-submit shape.
                    raise VenueTransientError(f"{label}: HTTP {resp.status}: {body}")

                ctype = resp.headers.get("Content-Type", "")
                if "html" in ctype.lower():
                    last_exc = VenueTransientError(f"{label}: non-JSON (HTML) response")
                    if retry_transient and attempt < len(_BACKOFFS):
                        await _retry_sleep(_BACKOFFS[attempt])
                        continue
                    raise last_exc

                try:
                    # content_type=None: some upstreams answer with
                    # text/plain on success and aiohttp would refuse to
                    # parse it.
                    return await resp.json(content_type=None)
                except (ValueError, aiohttp.ContentTypeError) as exc:
                    raise SolanaResponseError(
                        f"{label}: HTTP 200 with unparseable JSON body"
                    ) from exc

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            # NOT repr(exc) and never the exception object itself: aiohttp's
            # ClientResponseError carries request_info, whose repr renders the
            # request headers — including an x-api-key. The message here is a
            # string we control.
            last_exc = VenueTransientError(
                f"{label}: {type(exc).__name__} attempt={attempt + 1}"
            )
            if retry_transient and attempt < len(_BACKOFFS):
                log.warning(
                    "solana_http_retry",
                    label=label,
                    attempt=attempt + 1,
                    error_type=type(exc).__name__,
                )
                await _retry_sleep(_BACKOFFS[attempt])
                continue
            raise last_exc from None

    # Only reachable if the ladder is exhausted without a raise.
    raise last_exc or VenueTransientError(f"{label}: exhausted retries")


def require_field(payload: dict[str, Any], key: str, *, label: str) -> Any:
    """Return ``payload[key]`` or fail closed.

    A money-moving payload with a missing field is not a payload with a
    default — it is one we do not understand.
    """
    if not isinstance(payload, dict) or key not in payload or payload[key] is None:
        raise SolanaResponseError(f"{label}: response missing required field {key!r}")
    return payload[key]


def require_int(payload: dict[str, Any], key: str, *, label: str) -> int:
    """``require_field`` + a strict int coercion.

    Jupiter sends token amounts as decimal STRINGS and lamport fields as JSON
    numbers, so both have to be accepted — but a float that is not integral
    is rejected rather than truncated, because silently flooring a lamport
    amount changes what gets signed.
    """
    raw = require_field(payload, key, label=label)
    if isinstance(raw, bool):
        raise SolanaResponseError(f"{label}: field {key!r} is a bool, expected integer")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if not raw.is_integer():
            raise SolanaResponseError(
                f"{label}: field {key!r} is a non-integral float ({raw})"
            )
        return int(raw)
    if isinstance(raw, str):
        try:
            return int(raw)
        except ValueError as exc:
            raise SolanaResponseError(
                f"{label}: field {key!r} is not an integer string ({raw!r})"
            ) from exc
    raise SolanaResponseError(
        f"{label}: field {key!r} has unusable type {type(raw).__name__}"
    )
