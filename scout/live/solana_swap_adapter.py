"""SolanaSwapAdapter — ExchangeAdapter whose venue is the Jupiter aggregator.

The only component that knows the venue is on-chain. Maps the CEX-shaped
ExchangeAdapter contract onto Jupiter quotes + Solana RPC. venue_pair IS the
token mint string. Buy = USDC->mint; Sell = mint->USDC.
"""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog
from solders.transaction import VersionedTransaction

from scout.live.adapter_base import (
    ExchangeAdapter,
    OrderConfirmation,
    OrderRequest,
    VenueMetadata,
)
from scout.live.solana import constants as c
from scout.live.types import Depth, DepthLevel

log = structlog.get_logger(__name__)


class SolanaSwapAdapter(ExchangeAdapter):
    venue_name: str = "solana"
    is_onchain: bool = True

    def __init__(
        self, *, settings, jupiter, rpc, signer, db: Any | None = None
    ) -> None:
        self._settings = settings
        self._jupiter = jupiter
        self._rpc = rpc
        self._signer = signer
        self._db = db
        self._pending: dict = {}

    # ---- legacy / unused on this venue ----
    async def fetch_exchange_info_row(self, pair: str) -> dict | None:
        raise NotImplementedError("on-chain venue has no exchangeInfo")

    async def send_order(self, *, pair: str, side: str, size_usd: Decimal) -> dict:
        raise NotImplementedError("use place_order_request")

    # ---- metadata / resolution ----
    async def resolve_pair_for_symbol(self, symbol: str) -> str | None:
        meta = await self.fetch_venue_metadata(symbol)
        return meta.venue_pair if meta is not None else None

    async def fetch_venue_metadata(self, canonical: str) -> VenueMetadata | None:
        # canonical here is the SPL mint address. Routable if Jupiter quotes it.
        try:
            await self._jupiter.get_quote(
                input_mint=c.USDC_MINT,
                output_mint=canonical,
                amount=c.usdc_to_base_units(1.0),
                slippage_bps=self._settings.SOLANA_SLIPPAGE_BPS_CAP,
            )
        except Exception:
            return None
        return VenueMetadata(
            venue="solana",
            canonical=canonical,
            venue_pair=canonical,
            quote="USDC",
            asset_class="spot",
            min_size=None,
            tick_size=None,
            lot_size=None,
        )

    def _mints_for_side(self, venue_pair: str, side: str) -> tuple[str, str]:
        if side == "buy":
            return c.USDC_MINT, venue_pair
        return venue_pair, c.USDC_MINT

    async def quote_at_size(
        self, *, venue_pair: str, side: str, size_usd: float
    ) -> dict[str, Any]:
        input_mint, output_mint = self._mints_for_side(venue_pair, side)
        amount = c.usdc_to_base_units(size_usd)  # input is USDC for buy
        quote = await self._jupiter.get_quote(
            input_mint=input_mint,
            output_mint=output_mint,
            amount=amount,
            slippage_bps=self._settings.SOLANA_SLIPPAGE_BPS_CAP,
        )
        out_amount = int(quote["outAmount"])
        # Jupiter priceImpactPct is a fraction string (0.0042 == 0.42%).
        price_impact_pct = float(quote.get("priceImpactPct") or 0.0) * 100.0
        # I1: mid is WHOLE USDC per output-token BASE unit (size_usd /
        # out_amount), matching await_fill_confirmation's fill_price scale so
        # shadow mid_at_entry and live entry_fill_price are directly
        # comparable. NOTE: this is NOT normalized by the output token's
        # decimals — both shadow and live agree on this raw scale; decimals
        # normalization is a deferred follow-up (see await_fill_confirmation).
        mid = (
            Decimal(str(size_usd)) / Decimal(out_amount) if out_amount else Decimal("0")
        )
        return {
            "out_amount": out_amount,
            "price_impact_pct": price_impact_pct,
            "mid": mid,
        }

    async def fetch_price(self, pair: str) -> Decimal:
        # tiny-notional buy quote → mid (whole USDC per output-token base
        # unit; NOT decimals-normalized — see quote_at_size I1 note)
        q = await self.quote_at_size(venue_pair=pair, side="buy", size_usd=1.0)
        return q["mid"]

    async def fetch_depth(self, pair: str, limit: int = 100) -> Depth:
        # No order book on-chain. Synthesize a single-level Depth from the
        # at-size quote so generic callers still get a usable mid; the
        # on-chain gate uses price-impact directly (Task 11), not this walk.
        q = await self.quote_at_size(
            venue_pair=pair,
            side="buy",
            size_usd=float(self._settings.LIVE_TRADE_AMOUNT_USD),
        )
        mid = q["mid"] or Decimal("0")
        level = DepthLevel(price=mid, qty=Decimal("0"))
        return Depth(
            pair=pair,
            bids=(level,),
            asks=(level,),
            mid=mid,
            fetched_at=datetime.now(timezone.utc),
        )

    # ---- balances + sellability ----
    async def fetch_account_balance(self, asset: str = "USDT") -> float:
        owner = self._signer.pubkey() if self._signer is not None else None
        if owner is None:
            return 0.0
        if asset.upper() == "SOL":
            return await self._rpc.get_sol_balance(owner=owner)
        # NOTE: any non-SOL asset (incl. the CEX-default "USDT") maps to the
        # wallet's USDC balance — USDC is this venue's quote currency. This is a
        # deliberate symbol mapping, not a real USDT lookup.
        return await self._rpc.get_token_balance(owner=owner, mint=c.USDC_MINT)

    async def is_sellable(self, *, venue_pair: str, expected_out_amount: int) -> bool:
        """Honeypot guard: can we route AND simulate selling the position back
        to USDC? Any failure → not sellable → do not buy."""
        owner = self._signer.pubkey() if self._signer is not None else None
        if owner is None:
            return False
        try:
            sell_quote = await self._jupiter.get_quote(
                input_mint=venue_pair,
                output_mint=c.USDC_MINT,
                amount=expected_out_amount,
                slippage_bps=self._settings.SOLANA_SLIPPAGE_BPS_CAP,
            )
            tx_b64 = await self._jupiter.build_swap_tx(
                quote=sell_quote,
                user_pubkey=owner,
                priority_fee_lamports=self._settings.SOLANA_PRIORITY_FEE_LAMPORTS,
            )
            return await self._rpc.simulate_transaction(tx_b64)
        except Exception:
            log.info("solana_sellability_check_failed", venue_pair=venue_pair)
            return False

    # ---- order placement (two-phase: prepare then broadcast) ----
    @staticmethod
    def _signature_of_signed_tx(signed_tx_b64: str) -> str:
        """Derive the tx signature (base58 of the signed VersionedTransaction's
        first signature). Deterministic pre-broadcast — equals the signature
        the RPC will report once the tx lands."""
        raw = base64.b64decode(signed_tx_b64)
        tx = VersionedTransaction.from_bytes(raw)
        return str(tx.signatures[0])

    async def prepare_order(self, request: OrderRequest) -> tuple[str, str]:
        """Quote → build → sign → derive signature. Does NOT broadcast.

        Returns ``(signature, signed_tx_b64)``. The signature is computed from
        the SIGNED transaction's first signature and is identical to what the
        RPC reports after send, so the engine can persist it to live_trades
        BEFORE broadcasting (crash-recovery invariant — a tx that lands but
        whose send-call raises is still recoverable by boot reconciliation).
        """
        if self._signer is None:
            raise RuntimeError("no signer")
        owner = self._signer.pubkey()
        input_mint, output_mint = self._mints_for_side(request.venue_pair, request.side)
        amount = c.usdc_to_base_units(request.size_usd)  # USDC in for buy
        quote = await self._jupiter.get_quote(
            input_mint=input_mint,
            output_mint=output_mint,
            amount=amount,
            slippage_bps=self._settings.SOLANA_SLIPPAGE_BPS_CAP,
        )
        unsigned = await self._jupiter.build_swap_tx(
            quote=quote,
            user_pubkey=owner,
            priority_fee_lamports=self._settings.SOLANA_PRIORITY_FEE_LAMPORTS,
        )
        signed = self._signer.sign(unsigned)
        signature = self._signature_of_signed_tx(signed)
        self._pending[signature] = {
            "out_amount": int(quote["outAmount"]),
            "size_usd": request.size_usd,
            "side": request.side,
            "signed_tx_b64": signed,
        }
        log.info(
            "solana_order_prepared",
            signature=signature,
            side=request.side,
            size_usd=request.size_usd,
        )
        return signature, signed

    async def broadcast_prepared(self, signed_tx_b64: str) -> str:
        """Broadcast a previously-prepared signed transaction. Returns the
        signature the RPC reports (equal to the prepared signature)."""
        signature = await self._rpc.send_raw_transaction(signed_tx_b64)
        log.info("solana_order_sent", signature=signature)
        return signature

    async def place_order_request(self, request: OrderRequest) -> str:
        """Back-compat single-call place: prepare then broadcast. Returns the
        broadcast signature."""
        _, signed = await self.prepare_order(request)
        return await self.broadcast_prepared(signed)

    # ---- fill confirmation ----
    async def await_fill_confirmation(
        self,
        *,
        venue_order_id: str,
        client_order_id: str,
        timeout_sec: float,
        poll_interval_sec: float = 0.5,
        _sleep=None,
    ) -> OrderConfirmation:
        sleep = _sleep or asyncio.sleep
        pending = self._pending.get(venue_order_id, {})
        out_amount = pending.get("out_amount")
        size_usd = pending.get("size_usd")
        max_polls = max(1, int(timeout_sec / poll_interval_sec))
        status = "pending"
        for _ in range(max_polls):
            status = await self._rpc.confirm_signature(venue_order_id)
            if status in ("success", "failed"):
                break
            await sleep(poll_interval_sec)
        # Every path below is terminal for this signature; release the stash so
        # _pending does not grow unbounded for the process lifetime.
        self._pending.pop(venue_order_id, None)
        if status == "success":
            # I1: fill_price is WHOLE USDC per output-token BASE unit
            # (size_usd / out_amount), the canonical scale shared with
            # quote_at_size's mid. NOT normalized by the output token's
            # decimals — deferred follow-up; shadow and live agree on this
            # raw scale so there is no cross-path landmine in the meantime.
            fill_price = (size_usd / out_amount) if (out_amount and size_usd) else None
            return OrderConfirmation(
                venue="solana",
                venue_order_id=venue_order_id,
                client_order_id=client_order_id,
                status="filled",
                filled_qty=float(out_amount) if out_amount else None,
                fill_price=fill_price,
                raw_response={"signature": venue_order_id},
            )
        if status == "failed":
            return OrderConfirmation(
                venue="solana",
                venue_order_id=venue_order_id,
                client_order_id=client_order_id,
                status="rejected",
                filled_qty=None,
                fill_price=None,
                raw_response={
                    "signature": venue_order_id,
                    "reason": "on_chain_failure",
                },
            )
        return OrderConfirmation(
            venue="solana",
            venue_order_id=venue_order_id,
            client_order_id=client_order_id,
            status="timeout",
            filled_qty=None,
            fill_price=None,
            raw_response={"signature": venue_order_id},
        )
