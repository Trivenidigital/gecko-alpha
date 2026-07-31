"""Solana JSON-RPC client — READS AND SIMULATION ONLY (PR-S1).

*** THIS MODULE HAS NO BROADCAST METHOD AND MUST NEVER GAIN ONE. ***

There is deliberately no ``send_transaction`` / ``sendRawTransaction`` /
``sendTransaction`` here. That absence is the lane's structural guarantee that
it cannot broadcast through a public RPC: not a flag that defaults off, not a
policy in a docstring, but the absence of any code to call. The one submission
path is ``scout.live.solana.jito_client``. A test asserts this class has no
attribute named ``send_transaction`` precisely so the guarantee survives a
future well-meaning refactor.

Reads and simulation through a public RPC are expected and safe — they move
nothing.

Solana RPC facts verified against current docs on 2026-07-31
--------------------------------------------------------------
1. ``simulateTransaction`` (https://solana.com/docs/rpc/http/simulatetransaction).
   Config: ``commitment``, ``encoding`` (base64 recommended),
   ``replaceRecentBlockhash``, ``sigVerify``, ``minContextSlot``,
   ``innerInstructions``, ``accounts``. **``sigVerify`` and
   ``replaceRecentBlockhash`` conflict and cannot both be true** — that
   constraint is enforced locally in ``simulate_transaction`` rather than
   discovered as an RPC error. The transaction "is not required to be signed
   unless ``sigVerify`` is enabled". Result carries ``err`` (null on success),
   ``logs``, ``unitsConsumed``, plus ``fee`` / ``accounts`` / ``returnData`` /
   ``innerInstructions`` / ``loadedAddresses`` / ``replacementBlockhash``.

2. ``getSignatureStatuses`` (https://solana.com/docs/rpc/http/getsignaturestatuses).
   Takes up to 256 base-58 signatures plus optional
   ``{"searchTransactionHistory": bool}``. Each result element is EITHER
   ``null`` (signature unknown to the node) OR an object with ``slot``,
   ``confirmations`` (null once "rooted and finalized by a supermajority of
   stake"), ``err`` and ``confirmationStatus``
   (``processed`` / ``confirmed`` / ``finalized``).

   *** The ``null`` element is the fact the §7 resolver is built on, and it
   is WEAK evidence on its own. *** Without ``searchTransactionHistory`` the
   node "only searches the recent status cache", so ``null`` means "not in
   this node's recent cache" — NOT "never existed". The resolver therefore
   passes ``searchTransactionHistory=True`` and still refuses to call a
   missing signature definitive until the blockhash has also expired.

3. Address lookup tables. Resolved via ``getAccountInfo`` /
   ``getMultipleAccounts`` with ``encoding=base64``. An ALT account's layout
   is a 56-byte metadata prefix (``LOOKUP_TABLE_META_SIZE``) followed by a
   packed array of 32-byte addresses, so address *i* is
   ``data[56 + 32*i : 88 + 32*i]``. A trailing partial chunk means the
   account is not a well-formed table and is rejected rather than truncated.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import aiohttp
import structlog

from scout.config import Settings
from scout.live.solana.exceptions import (
    SolanaAPIError,
    SolanaResponseError,
    SolanaSimulationError,
)
from scout.live.solana.transport import request_json, require_field

log = structlog.get_logger(__name__)

# Byte offset at which an address lookup table's packed address array starts.
LOOKUP_TABLE_META_SIZE = 56
_PUBKEY_BYTES = 32

# Simulation logs are unbounded and end up in evidence files; keep the tail.
_LOG_TAIL_LINES = 20


@dataclass(frozen=True)
class SimulationResult:
    """Outcome of ``simulateTransaction``."""

    err: Any | None
    logs: tuple[str, ...]
    units_consumed: int | None
    slot: int | None

    @property
    def ok(self) -> bool:
        return self.err is None


@dataclass(frozen=True)
class SignatureStatus:
    """One element of ``getSignatureStatuses``.

    ``known=False`` models the ``null`` element: the node has no record of the
    signature. It is NOT proof the transaction never existed — see module
    docstring fact 2.
    """

    known: bool
    slot: int | None = None
    confirmations: int | None = None
    err: Any | None = None
    confirmation_status: str | None = None

    @property
    def landed(self) -> bool:
        """Known to the cluster and executed WITHOUT error."""
        return self.known and self.err is None

    @property
    def failed_on_chain(self) -> bool:
        """Known to the cluster and executed WITH an error.

        Distinct from ``not landed``: the transaction did run, the fee was
        paid, and it is final. Nothing should be resubmitted.
        """
        return self.known and self.err is not None


class SolanaRpcClient:
    """Read-only Solana JSON-RPC client. See the module docstring."""

    def __init__(self, settings: Settings, session: aiohttp.ClientSession) -> None:
        self._settings = settings
        self._session = session
        self._url = settings.SOLANA_RPC_URL
        self._timeout = float(settings.SOLANA_HTTP_TIMEOUT_SEC)
        self._request_id = 0

    async def _rpc(
        self,
        method: str,
        params: list[Any] | None = None,
        *,
        retry: bool = True,
        allow_null_result: bool = False,
    ) -> Any:
        """One JSON-RPC call. Returns the ``result`` member.

        A JSON-RPC ``error`` member is a definitive refusal by the node and is
        raised as ``SolanaAPIError``. HTTP-level anomalies are classified by
        ``transport.request_json`` — with ``four_xx_is_definitive=False``,
        because this is a JSON-RPC upstream whose real refusals arrive as
        HTTP 200.

        Args:
            allow_null_result: treat ``"result": null`` as a legitimate
                answer rather than a malformed payload. True only for methods
                that use null to mean "no such record" — ``getTransaction``
                answers that way for an unknown signature, and reading it as a
                protocol violation would make a missing transaction
                indistinguishable from a broken node.
        """
        self._request_id += 1
        body = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": method,
            "params": params or [],
        }
        payload = await request_json(
            self._session,
            "POST",
            self._url,
            label=f"solana_rpc.{method}",
            timeout_sec=self._timeout,
            json_body=body,
            retry_transient=retry,
            four_xx_is_definitive=False,
        )
        if not isinstance(payload, dict):
            raise SolanaResponseError(
                f"solana_rpc.{method}: response was not an object"
            )
        if "error" in payload and payload["error"] is not None:
            raise SolanaAPIError(f"solana_rpc.{method}: {payload['error']}")
        if allow_null_result and payload.get("result", "__absent__") is None:
            return None
        return require_field(payload, "result", label=f"solana_rpc.{method}")

    async def get_latest_blockhash(
        self, *, commitment: str = "confirmed"
    ) -> tuple[str, int]:
        """Return ``(blockhash, lastValidBlockHeight)``."""
        result = await self._rpc("getLatestBlockhash", [{"commitment": commitment}])
        value = require_field(result, "value", label="getLatestBlockhash")
        return (
            str(require_field(value, "blockhash", label="getLatestBlockhash")),
            int(
                require_field(value, "lastValidBlockHeight", label="getLatestBlockhash")
            ),
        )

    async def get_block_height(self, *, commitment: str = "confirmed") -> int:
        """Current block height — compared against ``lastValidBlockHeight``.

        Block HEIGHT, not slot. The two diverge (skipped slots do not advance
        height) and ``lastValidBlockHeight`` is denominated in height, so
        comparing it against a slot would expire transactions early.
        """
        return int(await self._rpc("getBlockHeight", [{"commitment": commitment}]))

    async def simulate_transaction(
        self,
        tx_b64: str,
        *,
        sig_verify: bool = False,
        replace_recent_blockhash: bool = False,
        commitment: str = "confirmed",
    ) -> SimulationResult:
        """Simulate a transaction without broadcasting it.

        Two intended call shapes:

        - **pre-sign** (``sig_verify=False``): does the swap execute against
          current chain state? The transaction need not be signed.
        - **post-sign** (``sig_verify=True``): the same, plus proof the
          signature we just produced actually verifies — a local check of our
          own signing, independent of solders.

        ``replace_recent_blockhash`` must stay False whenever ``sig_verify``
        is True (the RPC rejects the combination), and should stay False for
        the post-sign check regardless: replacing the blockhash would
        simulate a DIFFERENT transaction than the one being submitted.

        Raises:
            ValueError: both flags set — caught locally, not at the RPC.
            SolanaSimulationError: never. A failed simulation is RETURNED, not
                raised, so the caller can log ``logs``/``unitsConsumed`` into
                the evidence record. Use ``raise_for_simulation`` to convert.
        """
        if sig_verify and replace_recent_blockhash:
            raise ValueError(
                "sigVerify and replaceRecentBlockhash cannot both be true "
                "(Solana RPC rejects the combination)"
            )
        config = {
            "encoding": "base64",
            "commitment": commitment,
            "sigVerify": sig_verify,
            "replaceRecentBlockhash": replace_recent_blockhash,
        }
        result = await self._rpc("simulateTransaction", [tx_b64, config])
        value = require_field(result, "value", label="simulateTransaction")
        context = result.get("context") or {}
        logs = value.get("logs") or []
        sim = SimulationResult(
            err=value.get("err"),
            logs=tuple(str(line) for line in logs),
            units_consumed=(
                int(value["unitsConsumed"])
                if value.get("unitsConsumed") is not None
                else None
            ),
            slot=int(context["slot"]) if context.get("slot") is not None else None,
        )
        if sim.ok:
            log.info(
                "solana_simulation_ok",
                units_consumed=sim.units_consumed,
                slot=sim.slot,
                sig_verify=sig_verify,
            )
        else:
            log.error(
                "solana_simulation_failed",
                err=str(sim.err),
                units_consumed=sim.units_consumed,
                slot=sim.slot,
                sig_verify=sig_verify,
                logs_tail=list(sim.logs[-_LOG_TAIL_LINES:]),
            )
        return sim

    @staticmethod
    def raise_for_simulation(sim: SimulationResult) -> None:
        """Turn a failed simulation into ``SolanaSimulationError``."""
        if not sim.ok:
            raise SolanaSimulationError(
                f"simulation failed: err={sim.err}; " f"logs_tail={list(sim.logs[-5:])}"
            )

    async def get_signature_statuses(
        self, signatures: list[str], *, search_transaction_history: bool = True
    ) -> list[SignatureStatus]:
        """Status per signature, positionally aligned with ``signatures``.

        ``search_transaction_history`` defaults TRUE here, unlike the RPC's own
        default. The resolver's whole job is to distinguish "never landed"
        from "landed and fell out of the recent cache", and the recent cache
        is far shorter than the window in which an operator resolves an
        ambiguous submission.
        """
        if not signatures:
            return []
        result = await self._rpc(
            "getSignatureStatuses",
            [signatures, {"searchTransactionHistory": search_transaction_history}],
        )
        value = require_field(result, "value", label="getSignatureStatuses")
        if not isinstance(value, list) or len(value) != len(signatures):
            raise SolanaResponseError(
                "getSignatureStatuses: expected one element per signature; "
                f"asked {len(signatures)}, got "
                f"{len(value) if isinstance(value, list) else type(value).__name__}"
            )
        statuses: list[SignatureStatus] = []
        for entry in value:
            if entry is None:
                statuses.append(SignatureStatus(known=False))
                continue
            statuses.append(
                SignatureStatus(
                    known=True,
                    slot=int(entry["slot"]) if entry.get("slot") is not None else None,
                    confirmations=(
                        int(entry["confirmations"])
                        if entry.get("confirmations") is not None
                        else None
                    ),
                    err=entry.get("err"),
                    confirmation_status=entry.get("confirmationStatus"),
                )
            )
        return statuses

    async def get_balance(self, pubkey: str, *, commitment: str = "confirmed") -> int:
        """Native SOL balance in lamports."""
        result = await self._rpc("getBalance", [pubkey, {"commitment": commitment}])
        return int(require_field(result, "value", label="getBalance"))

    async def get_token_balance(
        self, owner: str, mint: str, *, commitment: str = "confirmed"
    ) -> int:
        """Summed raw token balance across ``owner``'s accounts for ``mint``.

        Summed rather than "the ATA's balance": an owner can hold the same
        mint in several accounts, and a reconciliation that reads only the
        canonical ATA would under-report a swap whose output was routed to a
        different one.

        Returns 0 when no token account exists — the correct reading of "owns
        none of this mint", and the expected state before a first USDC buy.
        """
        result = await self._rpc(
            "getTokenAccountsByOwner",
            [
                owner,
                {"mint": mint},
                {"encoding": "jsonParsed", "commitment": commitment},
            ],
        )
        accounts = require_field(result, "value", label="getTokenAccountsByOwner")
        if not isinstance(accounts, list):
            raise SolanaResponseError("getTokenAccountsByOwner: value was not a list")
        total = 0
        for acct in accounts:
            try:
                info = acct["account"]["data"]["parsed"]["info"]
                total += int(info["tokenAmount"]["amount"])
            except (KeyError, TypeError, ValueError) as exc:
                raise SolanaResponseError(
                    "getTokenAccountsByOwner: unparseable token account entry"
                ) from exc
        return total

    async def get_transaction(
        self, signature: str, *, commitment: str = "confirmed"
    ) -> dict[str, Any] | None:
        """Full transaction record for final reconciliation, or None if absent.

        ``maxSupportedTransactionVersion=0`` is required: without it the node
        refuses to return v0 transactions, and every transaction this lane
        builds is v0.
        """
        result = await self._rpc(
            "getTransaction",
            [
                signature,
                {
                    "encoding": "jsonParsed",
                    "commitment": commitment,
                    "maxSupportedTransactionVersion": 0,
                },
            ],
            # A null result means "no such transaction", not a broken node.
            allow_null_result=True,
        )
        if result is None:
            return None
        if not isinstance(result, dict):
            raise SolanaResponseError("getTransaction: result was not an object")
        return result

    async def fetch_address_lookup_tables(
        self, table_keys: list[str], *, commitment: str = "confirmed"
    ) -> dict[str, list[str]]:
        """Resolve ALT accounts to their address arrays.

        Only tables that resolve cleanly appear in the result. A missing or
        malformed table is OMITTED rather than returned empty, because
        ``tx_inspector`` distinguishes "this table resolved and contains no
        such index" from "we could not read this table" — and only the former
        is evidence about the transaction.

        Layout per module docstring fact 3.
        """
        if not table_keys:
            return {}
        result = await self._rpc(
            "getMultipleAccounts",
            [table_keys, {"encoding": "base64", "commitment": commitment}],
        )
        value = require_field(result, "value", label="getMultipleAccounts")
        if not isinstance(value, list) or len(value) != len(table_keys):
            raise SolanaResponseError(
                "getMultipleAccounts: expected one element per key; "
                f"asked {len(table_keys)}, got "
                f"{len(value) if isinstance(value, list) else type(value).__name__}"
            )

        tables: dict[str, list[str]] = {}
        for key, account in zip(table_keys, value):
            if account is None:
                log.warning("solana_alt_missing", table=key)
                continue
            addresses = self._decode_lookup_table(key, account)
            if addresses is not None:
                tables[key] = addresses
        return tables

    @staticmethod
    def _decode_lookup_table(key: str, account: Any) -> list[str] | None:
        """Decode one ALT account, or None if it is not a well-formed table."""
        import base58  # local import: only the ALT path needs it

        try:
            data_field = account["data"]
            raw = base64.b64decode(data_field[0])
        except (KeyError, TypeError, IndexError, ValueError):
            log.warning("solana_alt_undecodable", table=key)
            return None

        if len(raw) < LOOKUP_TABLE_META_SIZE:
            log.warning("solana_alt_too_short", table=key, length=len(raw))
            return None
        body = raw[LOOKUP_TABLE_META_SIZE:]
        if len(body) % _PUBKEY_BYTES:
            # A trailing partial pubkey means this is not a lookup table (or
            # we are decoding it wrong). Truncating would silently drop the
            # last address and let an unknown account through the inspector.
            log.warning("solana_alt_ragged", table=key, body_length=len(body))
            return None
        return [
            base58.b58encode(body[i : i + _PUBKEY_BYTES]).decode()
            for i in range(0, len(body), _PUBKEY_BYTES)
        ]
