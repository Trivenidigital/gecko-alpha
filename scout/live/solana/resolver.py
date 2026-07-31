"""§7 ambiguity resolver — what happened to a submission we lost track of.

The mandatory follow-up to ``SolanaAmbiguousSubmissionError``. Mirrors the
Kraken lane's ``resolve_order_submission`` (``kraken_adapter.py``): the
verdict is derived ONLY from what the cluster reports NOW, and the original
send's failure is never evidence. A timeout, a 502 and a dropped connection
are all fully compatible with the transaction having been forwarded and
landed — inferring "it never went" from the send failing is exactly the
reasoning that produces a duplicate swap.

The four verdicts
-----------------
``landed``
    Signature known to the cluster with ``err = null``. The swap executed.

``failed_on_chain``
    Signature known with a non-null ``err``. The transaction DID run, the fee
    was paid, and it is final. Nothing to resubmit — a rebuild here would be a
    second swap, not a retry.

``definitively_not_submitted``
    The strong one, and the only verdict that licenses a rebuild. Requires
    BOTH:
      1. the signature is absent from the cluster (searching transaction
         history, not just the recent cache), AND
      2. the current block height has passed ``last_valid_block_height`` — so
         the transaction's blockhash has expired and it can NEVER land.
    Condition 2 is what makes the verdict safe. Absence alone is not
    evidence: a transaction in flight is absent right up until it lands.
    Once the blockhash has expired, absence is permanent by protocol.

``unresolved``
    Everything else, including every probe error. The honest answer when we
    could not look, and it is deliberately easy to reach.

Why this keys off the SIGNATURE and not Jito's bundle status
-------------------------------------------------------------
The signature is derived locally from the message and the key BEFORE the POST
(see ``signer``), so it exists and is valid regardless of what the POST
returned. Jito's bundle id arrives in a response HEADER — a submission whose
response never arrived has no bundle id at all, which is precisely the case
this resolver exists to handle. Jito's status methods are supplementary
evidence, never the authority.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

import structlog

from scout.config import Settings
from scout.live.solana.constants import SOL_MINT, USDC_MINT

log = structlog.get_logger(__name__)

Verdict = Literal[
    "landed", "failed_on_chain", "definitively_not_submitted", "unresolved"
]

# Two sweeps either side of a settle delay, same as the Kraken lane. One
# sweep cannot distinguish "in flight" from "never sent".
_RESOLUTION_SWEEPS = 2


@dataclass(frozen=True)
class ResolutionProbe:
    """One sweep's observation. ``outcome`` is what we SAW, not what it means."""

    sweep: int
    outcome: Literal["landed", "failed_on_chain", "absent", "error"]
    block_height: int | None = None
    blockhash_expired: bool | None = None
    slot: int | None = None
    confirmation_status: str | None = None
    err: Any | None = None
    error_type: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class ResolutionReport:
    """Verdict plus the full evidence trail, for the authorization log."""

    verdict: Verdict
    signature: str
    last_valid_block_height: int | None
    probes: tuple[ResolutionProbe, ...]
    checked_at: str
    slot: int | None = None
    confirmation_status: str | None = None
    on_chain_err: Any | None = None
    balances: dict[str, int] = field(default_factory=dict)

    @property
    def rebuild_is_safe(self) -> bool:
        """Only one verdict licenses building and signing a replacement."""
        return self.verdict == "definitively_not_submitted"


async def resolve_submission(
    *,
    expected_signature: str,
    last_valid_block_height: int | None,
    rpc_client: Any,
    settings: Settings,
    owner_pubkey: str | None = None,
    input_mint: str = SOL_MINT,
    output_mint: str = USDC_MINT,
) -> ResolutionReport:
    """Decide what happened to ``expected_signature``.

    Args:
        last_valid_block_height: from the Jupiter build. When None, the
            blockhash-expiry test cannot be run and ``definitively_not_submitted``
            becomes UNREACHABLE — absence stays ``unresolved`` forever, which
            is the correct fail-closed behaviour rather than a degraded one.
        owner_pubkey: when given, SOL and token balances are recorded as
            supplementary evidence. Balances are NEVER used to decide the
            verdict — a balance can move for unrelated reasons, and reading a
            swap into one is how a resolver invents a fill.

    Never raises on a probe failure: a failed probe becomes an ``error``
    outcome and, ultimately, the ``unresolved`` verdict.
    """
    probes: list[ResolutionProbe] = []
    verdict: Verdict = "unresolved"
    slot: int | None = None
    confirmation_status: str | None = None
    on_chain_err: Any | None = None
    settle = float(getattr(settings, "SOLANA_SUBMISSION_SETTLE_SEC", 5.0))

    for sweep in range(_RESOLUTION_SWEEPS):
        if sweep:
            await asyncio.sleep(settle)

        try:
            statuses = await rpc_client.get_signature_statuses(
                [expected_signature], search_transaction_history=True
            )
        except Exception as exc:
            probes.append(
                ResolutionProbe(
                    sweep=sweep,
                    outcome="error",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            )
            log.warning(
                "solana_resolution_probe",
                sweep=sweep,
                signature=expected_signature,
                outcome="error",
                error_type=type(exc).__name__,
            )
            # A probe that failed is "we could not look", which is a
            # different fact from "we looked and it is not there". No later
            # sweep can repair it into an absence claim.
            verdict = "unresolved"
            break

        status = statuses[0] if statuses else None

        if status is not None and status.known:
            if status.err is None:
                verdict = "landed"
                outcome: Literal["landed", "failed_on_chain"] = "landed"
            else:
                verdict = "failed_on_chain"
                outcome = "failed_on_chain"
            slot = status.slot
            confirmation_status = status.confirmation_status
            on_chain_err = status.err
            probes.append(
                ResolutionProbe(
                    sweep=sweep,
                    outcome=outcome,
                    slot=status.slot,
                    confirmation_status=status.confirmation_status,
                    err=status.err,
                )
            )
            log.info(
                "solana_resolution_probe",
                sweep=sweep,
                signature=expected_signature,
                outcome=outcome,
                slot=status.slot,
                confirmation_status=status.confirmation_status,
            )
            break

        # Absent from the cluster. Only meaningful alongside blockhash expiry.
        block_height: int | None = None
        expired: bool | None = None
        try:
            block_height = await rpc_client.get_block_height()
        except Exception as exc:
            probes.append(
                ResolutionProbe(
                    sweep=sweep,
                    outcome="error",
                    error_type=type(exc).__name__,
                    error=f"get_block_height failed: {exc}",
                )
            )
            log.warning(
                "solana_resolution_probe",
                sweep=sweep,
                signature=expected_signature,
                outcome="error",
                error_type=type(exc).__name__,
            )
            verdict = "unresolved"
            break

        if last_valid_block_height is not None:
            expired = block_height > last_valid_block_height

        probes.append(
            ResolutionProbe(
                sweep=sweep,
                outcome="absent",
                block_height=block_height,
                blockhash_expired=expired,
            )
        )
        log.info(
            "solana_resolution_probe",
            sweep=sweep,
            signature=expected_signature,
            outcome="absent",
            block_height=block_height,
            last_valid_block_height=last_valid_block_height,
            blockhash_expired=expired,
        )

        if not expired:
            # Absent but still landable — in flight, as far as we can tell.
            # Never a definitive verdict, and a second sweep cannot help.
            verdict = "unresolved"
            break
    else:
        # Every sweep completed with the signature cleanly absent AND the
        # blockhash expired in each. The transaction can never land.
        if all(p.outcome == "absent" and p.blockhash_expired for p in probes):
            verdict = "definitively_not_submitted"
        else:
            verdict = "unresolved"

    balances: dict[str, int] = {}
    if owner_pubkey:
        # Supplementary evidence only — never an input to the verdict.
        for label, coro in (
            ("sol_lamports", rpc_client.get_balance(owner_pubkey)),
            ("input_token", rpc_client.get_token_balance(owner_pubkey, input_mint)),
            ("output_token", rpc_client.get_token_balance(owner_pubkey, output_mint)),
        ):
            try:
                balances[label] = await coro
            except Exception as exc:
                log.warning(
                    "solana_resolution_balance_failed",
                    label=label,
                    error_type=type(exc).__name__,
                )

    report = ResolutionReport(
        verdict=verdict,
        signature=expected_signature,
        last_valid_block_height=last_valid_block_height,
        probes=tuple(probes),
        checked_at=datetime.now(timezone.utc).isoformat(),
        slot=slot,
        confirmation_status=confirmation_status,
        on_chain_err=on_chain_err,
        balances=balances,
    )
    log.info(
        "solana_submission_resolved",
        signature=expected_signature,
        verdict=verdict,
        sweeps=len(probes),
        rebuild_is_safe=report.rebuild_is_safe,
        last_valid_block_height=last_valid_block_height,
    )
    return report
