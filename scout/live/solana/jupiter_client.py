"""Jupiter Swap API client — quote and swap-transaction build (PR-S1).

Jupiter facts verified against current docs on 2026-07-31
----------------------------------------------------------
1. Base host. All plans, free and paid, are served from ``https://api.jup.ag``
   with the Swap API under ``/swap/v1``
   (https://developers.jup.ag/docs/api-reference/swap/quote). A keyless tier
   exists at 0.5 req/sec (30 req/min); a free portal key raises the limit and
   is sent as the ``x-api-key`` header. Both the base and the key are config
   (``JUPITER_API_BASE`` / ``JUPITER_API_KEY``).

   *** NOT FULLY VERIFIABLE — ``lite-api.jup.ag``. *** Jupiter's plans page
   documents only ``api.jup.ag`` and makes no mention of ``lite-api.jup.ag``,
   while third-party sources describe ``lite-api.jup.ag/swap/v1`` as a
   now-deprecated keyless host with a migration deadline that has passed. The
   published pages do not state whether it still resolves. The default here is
   therefore ``api.jup.ag``, which every current Jupiter page agrees on, and
   the host is config so a pilot-day surprise is an .env edit, not a deploy.

2. ``GET /quote`` (https://developers.jup.ag/docs/api-reference/swap/quote).
   Query params: ``inputMint``, ``outputMint``, ``amount`` (raw, before
   decimals), ``slippageBps`` (default 50), ``swapMode`` (default ExactIn),
   ``restrictIntermediateTokens`` (default TRUE), ``onlyDirectRoutes``
   (default false), ``maxAccounts`` (default 64), ``platformFeeBps``,
   ``dexes`` / ``excludeDexes``.

   Response REQUIRED fields: ``inputMint``, ``inAmount``, ``outputMint``,
   ``outAmount``, ``otherAmountThreshold``, ``swapMode``, ``slippageBps``,
   ``priceImpactPct``, ``routePlan``. OPTIONAL: ``contextSlot``,
   ``timeTaken``, ``platformFee``. Amounts are decimal STRINGS;
   ``slippageBps`` is a number.

   ``otherAmountThreshold`` is the minimum acceptable output under ExactIn —
   it is the slippage bound that will actually be enforced on-chain, and it
   is what the approval screen must show, not ``outAmount``.

3. ``POST /swap`` (https://developers.jup.ag/docs/api-reference/swap/swap).
   Body REQUIRES ``userPublicKey`` and ``quoteResponse`` (the quote object
   echoed back VERBATIM — Jupiter re-derives the route from it, so it must not
   be rebuilt field-by-field).

   ``prioritizationFeeLamports`` is a **oneOf** with three variants, confirmed
   identically by Jupiter's API reference and its OpenAPI schema:
     - ``{"priorityLevelWithMaxLamports": {"priorityLevel": "veryHigh|high|
       medium", "maxLamports": <u64>, "global": <bool>}}``
     - ``{"jitoTipLamports": <u64>}``            <-- the variant this lane uses
     - ``{"jitoTipLamportsWithPayer": {"lamports": <u64>, "payer": <pubkey>}}``
   The Jito variant is a BARE INTEGER under the ``jitoTipLamports`` key, NOT a
   nested object. Setting it makes Jupiter add a tip instruction to a Jito tip
   account inside the built transaction — which is why ``tx_inspector`` must
   verify the tip destination and amount rather than trusting the request.

   Response REQUIRED: ``swapTransaction`` (base64 string, UNSIGNED) and
   ``lastValidBlockHeight`` (u64). OPTIONAL: ``prioritizationFeeLamports``
   (u64, what Jupiter actually applied). ``computeUnitLimit`` and
   ``simulationError`` appear in examples but are NOT in the published
   response schema, so both are read defensively and neither is required.

4. Blockhash semantics. The transaction comes back carrying a blockhash
   Jupiter chose, paired with ``lastValidBlockHeight``. The transaction can
   never land once the chain's block height exceeds that value — which is the
   fact the §7 resolver uses to turn "no signature found" into the DEFINITIVE
   ``definitively_not_submitted`` verdict. The build is therefore perishable:
   between build and submit there is a bounded window, and a stale build must
   be re-quoted rather than re-signed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import aiohttp
import structlog

from scout.config import Settings
from scout.live.solana.constants import SOL_MINT, USDC_MINT
from scout.live.solana.exceptions import SolanaResponseError
from scout.live.solana.transport import request_json, require_field, require_int

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class RouteStep:
    """One hop of the route, flattened for the approval screen."""

    label: str
    amm_key: str
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int


@dataclass(frozen=True)
class SolanaQuote:
    """A parsed Jupiter quote.

    ``raw`` is the untouched response object. It is kept because ``POST
    /swap`` requires the quote echoed back VERBATIM — Jupiter re-derives the
    route from it, and a reconstructed object silently changes the route.
    """

    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    other_amount_threshold: int
    slippage_bps: int
    price_impact_pct: str
    swap_mode: str
    route_plan: tuple[RouteStep, ...]
    context_slot: int | None
    raw: dict[str, Any] = field(repr=False)

    @property
    def min_out_amount(self) -> int:
        """The output floor that will be enforced on-chain.

        Alias for ``other_amount_threshold`` under ExactIn. Named for what it
        means so callers do not reach for ``out_amount`` (an estimate) when
        they need the guarantee.
        """
        return self.other_amount_threshold


@dataclass(frozen=True)
class SwapBuild:
    """The unsigned transaction Jupiter built, plus its expiry bound."""

    swap_transaction_b64: str
    last_valid_block_height: int
    prioritization_fee_lamports: int | None
    compute_unit_limit: int | None
    simulation_error: Any | None
    requested_jito_tip_lamports: int


class JupiterClient:
    """Async client for Jupiter's quote and swap-build endpoints.

    Session is injected, never global (project convention). The client does
    NOT sign, submit, or hold key material — it turns intent into an unsigned
    transaction, which ``tx_inspector`` then has to be convinced by.
    """

    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self._settings = settings
        self._session = session
        self._base_url = settings.JUPITER_API_BASE.rstrip("/")
        self._timeout = float(settings.SOLANA_HTTP_TIMEOUT_SEC)

    def _headers(self) -> dict[str, str]:
        key = self._settings.JUPITER_API_KEY
        if key is None:
            return {}
        secret = key.get_secret_value()
        # A blank key is absence, not a credential — sending `x-api-key: ""`
        # gets the request rejected rather than falling back to the keyless
        # tier.
        return {"x-api-key": secret} if secret else {}

    async def get_quote(
        self,
        *,
        input_mint: str = SOL_MINT,
        output_mint: str = USDC_MINT,
        amount: int,
        slippage_bps: int | None = None,
        restrict_intermediate_tokens: bool = True,
        only_direct_routes: bool = False,
    ) -> SolanaQuote:
        """Fetch a route for ``amount`` raw units of ``input_mint``.

        Args:
            amount: RAW units before decimals (lamports for SOL).
            slippage_bps: Defaults to ``SOLANA_PILOT_SLIPPAGE_BPS``.
            restrict_intermediate_tokens: Left at Jupiter's own default of
                True. It confines multi-hop routes to liquid intermediate
                tokens, which is what stops a $10 swap being routed through
                a thin pool for a marginally better headline quote.

        Raises:
            SolanaResponseError: any required field missing or unusable.
            SolanaAPIError: Jupiter refused (HTTP 4xx — definitive for REST).
        """
        if amount <= 0:
            raise ValueError(f"quote amount must be > 0; got {amount}")

        bps = (
            slippage_bps
            if slippage_bps is not None
            else int(self._settings.SOLANA_PILOT_SLIPPAGE_BPS)
        )
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": str(bps),
            "restrictIntermediateTokens": (
                "true" if restrict_intermediate_tokens else "false"
            ),
            "onlyDirectRoutes": "true" if only_direct_routes else "false",
        }

        payload = await request_json(
            self._session,
            "GET",
            f"{self._base_url}/quote",
            label="jupiter_quote",
            timeout_sec=self._timeout,
            params=params,
            headers=self._headers(),
            retry_transient=True,
            four_xx_is_definitive=True,
        )
        if not isinstance(payload, dict):
            raise SolanaResponseError("jupiter_quote: response was not a JSON object")

        quote = self._parse_quote(payload)

        # Fail closed on a route that does not connect the mints we asked
        # for. Jupiter echoes the mints, so a mismatch means we are looking at
        # a different swap than the one intended — and every downstream check
        # (inspector, min-out) would then be validating the wrong thing.
        if quote.input_mint != input_mint or quote.output_mint != output_mint:
            raise SolanaResponseError(
                "jupiter_quote: mint mismatch — asked "
                f"{input_mint}->{output_mint}, got "
                f"{quote.input_mint}->{quote.output_mint}"
            )

        log.info(
            "jupiter_quote",
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=quote.in_amount,
            out_amount=quote.out_amount,
            min_out=quote.other_amount_threshold,
            slippage_bps=quote.slippage_bps,
            price_impact_pct=quote.price_impact_pct,
            hops=len(quote.route_plan),
            context_slot=quote.context_slot,
        )
        return quote

    @staticmethod
    def _parse_quote(payload: dict[str, Any]) -> SolanaQuote:
        label = "jupiter_quote"
        route_plan_raw = require_field(payload, "routePlan", label=label)
        if not isinstance(route_plan_raw, list) or not route_plan_raw:
            raise SolanaResponseError(f"{label}: routePlan is empty or not a list")

        steps: list[RouteStep] = []
        for idx, step in enumerate(route_plan_raw):
            if not isinstance(step, dict):
                raise SolanaResponseError(f"{label}: routePlan[{idx}] is not an object")
            info = require_field(step, "swapInfo", label=f"{label}.routePlan[{idx}]")
            if not isinstance(info, dict):
                raise SolanaResponseError(
                    f"{label}: routePlan[{idx}].swapInfo is not an object"
                )
            sub = f"{label}.routePlan[{idx}].swapInfo"
            steps.append(
                RouteStep(
                    # `label` is documented but not required; an unnamed AMM
                    # is a display gap, not a safety one.
                    label=str(info.get("label", "")),
                    amm_key=str(require_field(info, "ammKey", label=sub)),
                    input_mint=str(require_field(info, "inputMint", label=sub)),
                    output_mint=str(require_field(info, "outputMint", label=sub)),
                    in_amount=require_int(info, "inAmount", label=sub),
                    out_amount=require_int(info, "outAmount", label=sub),
                )
            )

        context_slot_raw = payload.get("contextSlot")
        context_slot = (
            require_int(payload, "contextSlot", label=label)
            if context_slot_raw is not None
            else None
        )

        return SolanaQuote(
            input_mint=str(require_field(payload, "inputMint", label=label)),
            output_mint=str(require_field(payload, "outputMint", label=label)),
            in_amount=require_int(payload, "inAmount", label=label),
            out_amount=require_int(payload, "outAmount", label=label),
            other_amount_threshold=require_int(
                payload, "otherAmountThreshold", label=label
            ),
            slippage_bps=require_int(payload, "slippageBps", label=label),
            price_impact_pct=str(require_field(payload, "priceImpactPct", label=label)),
            swap_mode=str(require_field(payload, "swapMode", label=label)),
            route_plan=tuple(steps),
            context_slot=context_slot,
            raw=payload,
        )

    async def build_swap_transaction(
        self,
        *,
        quote: SolanaQuote,
        user_pubkey: str,
        jito_tip_lamports: int,
        wrap_and_unwrap_sol: bool = True,
        dynamic_compute_unit_limit: bool = True,
    ) -> SwapBuild:
        """Ask Jupiter to build the unsigned swap transaction.

        The tip is requested via the ``{"jitoTipLamports": <u64>}`` variant of
        ``prioritizationFeeLamports`` (module docstring, fact 3), because the
        lane submits through Jito and a transaction with no tip will not be
        picked up by the auction.

        What comes back is UNSIGNED and UNTRUSTED. Requesting a tip of N does
        not establish that the built transaction tips N, or that it tips a
        Jito account at all — ``tx_inspector.verify_swap_transaction`` has to
        prove that from the transaction's own bytes before it is signed.

        Raises:
            SolanaResponseError: ``swapTransaction`` or
                ``lastValidBlockHeight`` missing/unusable.
            SolanaAPIError: Jupiter refused the build.
        """
        if jito_tip_lamports < 0:
            raise ValueError(f"jito_tip_lamports must be >= 0; got {jito_tip_lamports}")
        ceiling = int(self._settings.SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS)
        if jito_tip_lamports > ceiling:
            # Checked before the request as well as after the build: there is
            # no reason to ask Jupiter to construct something we have already
            # decided we will refuse to sign.
            raise ValueError(
                f"jito_tip_lamports {jito_tip_lamports} exceeds "
                f"SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS {ceiling}"
            )

        body: dict[str, Any] = {
            "userPublicKey": user_pubkey,
            # Echoed VERBATIM — see SolanaQuote.raw.
            "quoteResponse": quote.raw,
            "wrapAndUnwrapSol": wrap_and_unwrap_sol,
            "dynamicComputeUnitLimit": dynamic_compute_unit_limit,
            "prioritizationFeeLamports": {"jitoTipLamports": jito_tip_lamports},
        }

        payload = await request_json(
            self._session,
            "POST",
            f"{self._base_url}/swap",
            label="jupiter_swap",
            timeout_sec=self._timeout,
            json_body=body,
            headers=self._headers(),
            # A swap BUILD is a pure function call — it moves nothing and
            # holds no key — so retrying it is safe. The non-retryable step is
            # submission, in jito_client.
            retry_transient=True,
            four_xx_is_definitive=True,
        )
        if not isinstance(payload, dict):
            raise SolanaResponseError("jupiter_swap: response was not a JSON object")

        label = "jupiter_swap"
        tx_b64 = require_field(payload, "swapTransaction", label=label)
        if not isinstance(tx_b64, str) or not tx_b64:
            raise SolanaResponseError(
                f"{label}: swapTransaction is not a non-empty string"
            )

        applied_fee = payload.get("prioritizationFeeLamports")
        cu_limit = payload.get("computeUnitLimit")

        build = SwapBuild(
            swap_transaction_b64=tx_b64,
            last_valid_block_height=require_int(
                payload, "lastValidBlockHeight", label=label
            ),
            prioritization_fee_lamports=(
                require_int(payload, "prioritizationFeeLamports", label=label)
                if applied_fee is not None
                else None
            ),
            # Documented in examples but absent from the published schema
            # (module docstring, fact 3) — read defensively, never required.
            compute_unit_limit=(
                require_int(payload, "computeUnitLimit", label=label)
                if cu_limit is not None
                else None
            ),
            simulation_error=payload.get("simulationError"),
            requested_jito_tip_lamports=jito_tip_lamports,
        )

        log.info(
            "jupiter_swap_built",
            user_pubkey=user_pubkey,
            last_valid_block_height=build.last_valid_block_height,
            requested_jito_tip_lamports=jito_tip_lamports,
            prioritization_fee_lamports=build.prioritization_fee_lamports,
            compute_unit_limit=build.compute_unit_limit,
            has_simulation_error=build.simulation_error is not None,
            tx_b64_len=len(tx_b64),
        )
        return build
