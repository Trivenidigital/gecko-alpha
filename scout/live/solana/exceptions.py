"""Typed exceptions for the Solana execution lane (PR-S1).

Mirrors the Kraken lane's taxonomy (``scout/live/kraken_adapter.py``): callers
discriminate on TYPE, never on the message string, and the split that matters
is DEFINITIVE vs AMBIGUOUS.

A definitive error proves the venue refused the request. An ambiguous one
proves nothing — the transaction may be in flight, may have landed, may never
have been received. On Solana the stakes of confusing the two are higher than
on a centralised venue: there is no client-order-id, no server-side dedup, and
a resent transaction with the SAME blockhash and signature is idempotent while
a REBUILT one is a second, independent swap. Hence
``SolanaAmbiguousSubmissionError`` carries the expected signature and the
lane never rebuilds or resubmits on it.
"""

from __future__ import annotations

from scout.live.exceptions import LiveError, RateLimitError


class SolanaError(LiveError):
    """Root of the Solana lane's exception hierarchy."""


class SolanaAPIError(SolanaError):
    """An upstream (Jupiter / RPC / Jito) refused the request.

    Terminal — the request was understood and declined. Never retried.
    """


class SolanaResponseError(SolanaAPIError):
    """A response was structurally unusable: missing field, wrong type, non-JSON.

    Fail-closed. A quote missing ``outAmount`` or a swap reply missing
    ``lastValidBlockHeight`` is not a smaller quote or a shorter-lived
    transaction — it is a payload we do not understand, and guessing a default
    for a money-moving field is how a lane silently trades on garbage.
    """


class SolanaRateLimitError(RateLimitError):
    """HTTP 429 from Jupiter, the RPC, or the Jito block engine.

    Subclasses the shared ``RateLimitError`` so existing engine-side handling
    keeps working. Not retried in-loop: Jito's default limit is 1 request per
    second per IP per region and Jupiter's keyless tier is 30/min, so an
    in-loop retry deepens the penalty rather than clearing it.
    """


class SolanaVerificationError(SolanaError):
    """``tx_inspector`` could not prove the built transaction matches intent.

    Hard reject, and deliberately the widest class in this module: it covers
    an unexpected signer, an unexpected mint, an unknown instruction
    destination, a tip or priority fee over its ceiling, an unresolvable
    address-lookup table — and anything the inspector simply does not
    recognise. Unknown is a FAILURE, not a pass. This is the last check
    standing between a remotely-built transaction and our signing key.
    """


class SolanaLimitBreached(SolanaError):
    """The trade is outside the lane's execution envelope.

    Raised by ``scout.live.solana.limits`` and distinct from
    ``SolanaVerificationError``: the inspector answers "are these bytes what we
    asked for", this answers "are we allowed to do this at all". A build can be
    perfectly well-formed and still exceed the daily cap.

    Terminal for the attempt. Nothing has been signed or sent when it is
    raised — every limit is evaluated before the funded key is read.
    """


class SolanaKeypairError(SolanaError):
    """The signing keypair could not be loaded, or its file is unsafe.

    Covers a missing/blank ``SOLANA_PILOT_KEYPAIR_PATH``, a malformed id.json,
    a file whose permissions are wider than 0600, and a file owned by another
    uid. Never carries the key material or the file's contents.
    """


class SolanaSimulationError(SolanaError):
    """``simulateTransaction`` returned a non-null ``err``.

    Terminal for this attempt. The transaction was executed against current
    chain state and failed — submitting it anyway burns a fee to land a
    failure.
    """


class SolanaAmbiguousSubmissionError(SolanaError):
    """A Jito submission may or may not have reached the block engine.

    Raised when the POST fails in a way that does NOT prove the transaction
    was refused: a timeout, a dropped connection, an HTTP 5xx, a CDN
    interstitial. All of those can occur AFTER the block engine accepted and
    forwarded the transaction.

    **Never rebuild and never resubmit on this.** Rebuilding produces a
    DIFFERENT transaction (fresh blockhash, fresh signature) that can land
    alongside the first one — two swaps where the operator authorised one.
    Resolve with ``scout.live.solana.resolver.resolve_submission`` instead,
    using the ``expected_signature`` carried here; it was derived locally
    before the POST and is valid regardless of what the POST returned.
    """

    def __init__(
        self,
        message: str,
        *,
        expected_signature: str,
        last_valid_block_height: int | None = None,
        bundle_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.expected_signature = expected_signature
        self.last_valid_block_height = last_valid_block_height
        # The block engine can return `x-bundle-id` on a response whose BODY
        # never arrived, so the ambiguous path is exactly where a bundle id is
        # most useful and most easily lost. Diagnostic only — the verdict is
        # always decided from the signature, because a submission whose
        # response was lost entirely has no bundle id at all.
        self.bundle_id = bundle_id
