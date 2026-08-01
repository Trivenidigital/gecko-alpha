"""Jito block-engine client — the lane's ONLY submission path (PR-S1).

Jito facts verified against current docs on 2026-07-31
--------------------------------------------------------
Source: https://docs.jito.wtf/lowlatencytxnsend/

1. **Single-transaction endpoint.** ``POST https://mainnet.block-engine.jito.wtf
   /api/v1/transactions``, JSON-RPC, method ``sendTransaction``, params
   ``[<encoded_tx>, {"encoding": "base64"}]``. base64 is "recommended";
   base58 is "slow, DEPRECATED" and is the parameter DEFAULT, so the encoding
   option must be sent explicitly rather than relied upon. Regional hosts
   (Amsterdam, Dublin, Frankfurt, London, New York, Salt Lake City, Singapore,
   Tokyo) exist; the host is config.

2. **How it differs from /api/v1/bundles.** ``/bundles`` accepts up to 5
   transactions executed atomically. ``/transactions`` handles one and "acts
   as a direct proxy to the Solana sendTransaction RPC method, but forwards
   your transaction directly to the validator", with ``skip_preflight=true``.
   *** skip_preflight=true is why independent simulation is mandatory before
   submitting: the block engine will not catch a transaction that fails. ***

3. **``bundleOnly=true``** (query param) sends the transaction "exclusively as
   a single transaction bundle", which is what buys revert protection. The
   lane defaults it ON — a supervised pilot would rather not land than land a
   reverted swap and pay for it.

4. **Tips.** Minimum 1000 lamports, with the docs warning that under
   high demand "this minimum tip might not be sufficient to successfully
   navigate the auction". For ``sendTransaction`` the docs suggest a 70/30
   split between priority fee and Jito tip. The tip is an instruction INSIDE
   the transaction (added by Jupiter via ``jitoTipLamports``), not a field
   here.

5. **Auth.** No API key required: "You no longer need an approved auth key for
   default sends."

6. **Rate limit.** Default 1 request/second per IP per region, for both
   endpoints.

7. **Status surface — the important limitation.** ``getBundleStatuses``
   requires a bundle id and, for a ``/transactions`` submission, only works
   when it was sent with ``bundleOnly=true``. ``getInflightBundleStatuses``
   returns ``Invalid|Pending|Failed|Landed`` but only "within the last five
   minutes". The bundle id itself arrives in the ``x-bundle-id`` RESPONSE
   HEADER, not in the JSON body — so a submission whose response never
   arrived has NO bundle id and cannot be queried here at all.

   *** That is precisely why the §7 resolver keys off the SIGNATURE via the
   Solana RPC rather than off Jito's status surface. *** The signature is
   derived locally before submission and is valid whether or not the POST
   returned anything; the bundle id is not. Jito's status methods are a
   supplementary evidence source, never the authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import aiohttp
import structlog

from scout.config import Settings
from scout.live.exceptions import VenueTransientError
from scout.live.solana.constants import (
    JITO_TIP_ACCOUNTS_FALLBACK,
    JITO_TRANSACTIONS_PATH,
)
from scout.live.solana.exceptions import (
    SolanaAmbiguousSubmissionError,
    SolanaAPIError,
    SolanaRateLimitError,
    SolanaResponseError,
)
from scout.live.solana.transport import request_json, require_field

log = structlog.get_logger(__name__)

_BUNDLE_ID_HEADER = "x-bundle-id"


@dataclass(frozen=True)
class SubmissionReceipt:
    """Proof that the block engine ACKNOWLEDGED a transaction.

    ``signature`` is the locally-derived expected signature, echoed back for
    the caller's evidence record — not something the block engine told us.
    ``returned_signature`` is what Jito's JSON-RPC result carried, which for
    ``sendTransaction`` is the transaction signature; a mismatch between the
    two means the bytes submitted were not the bytes we signed.
    """

    signature: str
    returned_signature: str | None
    bundle_id: str | None
    bundle_only: bool
    raw: dict[str, Any] = field(repr=False, default_factory=dict)


@dataclass(frozen=True)
class BundleStatus:
    """One element of ``getInflightBundleStatuses`` / ``getBundleStatuses``."""

    bundle_id: str
    status: str | None
    landed_slot: int | None
    err: Any | None = None
    confirmation_status: str | None = None


class JitoClient:
    """Async client for the Jito block engine. See the module docstring."""

    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self._settings = settings
        self._session = session
        self._base_url = settings.JITO_BLOCK_ENGINE_URL.rstrip("/")
        self._timeout = float(settings.SOLANA_HTTP_TIMEOUT_SEC)
        self._request_id = 0

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    async def _rpc(
        self,
        path: str,
        method: str,
        params: list[Any],
        *,
        query: dict[str, str] | None = None,
        retry: bool,
        capture_headers: dict[str, str] | None = None,
    ) -> Any:
        body = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": method,
            "params": params,
        }
        payload = await request_json(
            self._session,
            "POST",
            f"{self._base_url}{path}",
            label=f"jito.{method}",
            timeout_sec=self._timeout,
            json_body=body,
            params=query,
            retry_transient=retry,
            # JSON-RPC upstream: real refusals arrive as HTTP 200 with an
            # `error` member, so a raw 4xx is transport of unknown provenance
            # and must stay AMBIGUOUS on the submit path.
            four_xx_is_definitive=False,
            capture_headers=capture_headers,
        )
        if not isinstance(payload, dict):
            raise SolanaResponseError(f"jito.{method}: response was not an object")
        if payload.get("error") is not None:
            raise SolanaAPIError(f"jito.{method}: {payload['error']}")
        return require_field(payload, "result", label=f"jito.{method}")

    async def submit_transaction(
        self,
        signed_tx_b64: str,
        *,
        expected_signature: str,
        last_valid_block_height: int | None = None,
        bundle_only: bool = True,
    ) -> SubmissionReceipt:
        """Submit ONE signed transaction. Exactly one POST, ever.

        There is no retry here and there must never be one. A timeout, a 5xx
        or a dropped connection does NOT prove the block engine failed to
        receive and forward the transaction, so a second POST is a second
        chance for the SAME transaction to land — harmless in itself, since a
        replayed identical signature is idempotent on Solana — but the retry
        ladder is not what makes this dangerous. What makes it dangerous is
        that a caller who sees a retry succeed after a timeout learns the
        wrong lesson and rebuilds. Rebuilding produces a different blockhash
        and a different signature, and BOTH can land.

        On any ambiguous outcome this raises ``SolanaAmbiguousSubmissionError``
        carrying ``expected_signature``, and the caller must resolve with
        ``resolver.resolve_submission`` rather than resend.

        Raises:
            SolanaAPIError: the block engine explicitly refused (JSON-RPC
                ``error`` member). DEFINITIVE — the transaction was not
                accepted.
            SolanaAmbiguousSubmissionError: everything else.
        """
        query = {"bundleOnly": "true"} if bundle_only else None
        headers: dict[str, str] = {}

        log.info(
            "solana_submission_attempt",
            expected_signature=expected_signature,
            bundle_only=bundle_only,
            last_valid_block_height=last_valid_block_height,
        )

        try:
            result = await self._rpc(
                JITO_TRANSACTIONS_PATH,
                "sendTransaction",
                # encoding sent explicitly: the param default is the
                # deprecated base58 (module docstring, fact 1).
                [signed_tx_b64, {"encoding": "base64"}],
                query=query,
                retry=False,
                capture_headers=headers,
            )
        except SolanaAPIError:
            # A JSON-RPC error member IS the block engine deciding. Definitive
            # rejection — re-raised unchanged so the caller does not treat it
            # as ambiguous and start probing for a transaction that was never
            # accepted.
            log.error(
                "solana_submission_rejected", expected_signature=expected_signature
            )
            raise
        except (VenueTransientError, SolanaRateLimitError, SolanaResponseError) as exc:
            bundle_id = headers.get(_BUNDLE_ID_HEADER)
            log.error(
                "solana_submission_ambiguous",
                expected_signature=expected_signature,
                bundle_id=bundle_id,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            raise SolanaAmbiguousSubmissionError(
                f"jito submission outcome unknown ({type(exc).__name__}: {exc}). "
                "Do NOT rebuild or resubmit — resolve via "
                "resolver.resolve_submission using the expected signature.",
                expected_signature=expected_signature,
                last_valid_block_height=last_valid_block_height,
            ) from exc

        returned_signature = result if isinstance(result, str) else None
        receipt = SubmissionReceipt(
            signature=expected_signature,
            returned_signature=returned_signature,
            bundle_id=headers.get(_BUNDLE_ID_HEADER),
            bundle_only=bundle_only,
            raw={"result": result},
        )

        if returned_signature is not None and returned_signature != expected_signature:
            # The block engine indexed a different transaction than the one we
            # signed. Ambiguous, not definitive: something WAS accepted, and
            # the safe move is to go resolve both rather than assume either.
            log.error(
                "solana_submission_signature_mismatch",
                expected_signature=expected_signature,
                returned_signature=returned_signature,
                bundle_id=receipt.bundle_id,
            )
            raise SolanaAmbiguousSubmissionError(
                "jito acknowledged a signature that is not the one we signed "
                f"(expected {expected_signature}, got {returned_signature}). "
                "Do NOT resubmit — resolve both signatures.",
                expected_signature=expected_signature,
                last_valid_block_height=last_valid_block_height,
            )

        log.info(
            "solana_submission_acknowledged",
            expected_signature=expected_signature,
            bundle_id=receipt.bundle_id,
            bundle_only=bundle_only,
        )
        return receipt

    async def fetch_tip_accounts(self) -> frozenset[str]:
        """Current mainnet tip accounts, falling back to the static list.

        The live list is preferred because Jito can rotate the accounts and a
        stale allowlist makes ``tx_inspector`` reject a legitimately-tipped
        transaction. The fallback exists so a block-engine outage does not
        block the pilot on a list that changes rarely.
        """
        try:
            result = await self._rpc(
                "/api/v1/getTipAccounts", "getTipAccounts", [], retry=True
            )
        except Exception as exc:
            log.warning(
                "jito_tip_accounts_fallback",
                error_type=type(exc).__name__,
                error=str(exc),
                count=len(JITO_TIP_ACCOUNTS_FALLBACK),
            )
            return JITO_TIP_ACCOUNTS_FALLBACK

        if not isinstance(result, list) or not all(isinstance(a, str) for a in result):
            log.warning(
                "jito_tip_accounts_unusable", count=len(JITO_TIP_ACCOUNTS_FALLBACK)
            )
            return JITO_TIP_ACCOUNTS_FALLBACK
        if not result:
            return JITO_TIP_ACCOUNTS_FALLBACK

        accounts = frozenset(result)
        log.info("jito_tip_accounts", count=len(accounts))
        return accounts

    async def get_inflight_bundle_statuses(
        self, bundle_ids: list[str]
    ) -> list[BundleStatus]:
        """Statuses for bundles submitted within the last five minutes.

        Supplementary evidence only — see module docstring fact 7. Outside the
        five-minute window this returns nothing useful, and a submission whose
        response never arrived has no bundle id to ask about.
        """
        if not bundle_ids:
            return []
        result = await self._rpc(
            "/api/v1/getInflightBundleStatuses",
            "getInflightBundleStatuses",
            [bundle_ids],
            retry=True,
        )
        return self._parse_bundle_statuses(result)

    async def get_bundle_statuses(self, bundle_ids: list[str]) -> list[BundleStatus]:
        """Final statuses for bundles. Requires ``bundleOnly=true`` submission."""
        if not bundle_ids:
            return []
        result = await self._rpc(
            "/api/v1/getBundleStatuses", "getBundleStatuses", [bundle_ids], retry=True
        )
        return self._parse_bundle_statuses(result)

    @staticmethod
    def _parse_bundle_statuses(result: Any) -> list[BundleStatus]:
        value = result.get("value") if isinstance(result, dict) else result
        if value is None:
            return []
        if not isinstance(value, list):
            raise SolanaResponseError("jito bundle statuses: value was not a list")
        statuses: list[BundleStatus] = []
        for entry in value:
            if not isinstance(entry, dict):
                continue
            statuses.append(
                BundleStatus(
                    bundle_id=str(entry.get("bundle_id", "")),
                    status=entry.get("status"),
                    landed_slot=(
                        int(entry["landed_slot"])
                        if entry.get("landed_slot") is not None
                        else None
                    ),
                    err=entry.get("err"),
                    confirmation_status=entry.get("confirmationStatus"),
                )
            )
        return statuses
