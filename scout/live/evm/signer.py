"""The EVM signer boundary — the key is not READ until the bundle is authorized.

The rule, generalized
---------------------
The Solana lane's guarantee is that the funded key is *not read* before the
operator authorizes: the signer's public key comes from configuration so the
transaction can be built, the fee payer checked and the balance read, all with the
key file untouched, and custody is verified by ``stat`` alone.

Permit2 needs TWO signatures, so "the message hash" is no longer a single thing to
bind. :class:`~scout.live.evm.signing_bundle.ExecutionSigningBundle` is the
generalization, and this module enforces the same ordering against it:

    build → decode → validate → simulate → bundle → **gate** → load key → sign

Every step before ``gate`` runs with ``EVM_SIGNER_KEY_PATH`` unopened.

Why the key loader is injected
-------------------------------
``key_loader`` is a callable supplied at construction rather than a module import.
That is what makes "the key was never read" a TESTABLE claim instead of an assured
one: a test passes a loader that records its calls (or explodes), and the assertion
is that it was called zero times. The Solana lane uses the same seam
(``keypair_loader``), and its ``loader_spy.calls == 0`` assertions are the reason
that guarantee is believed rather than hoped.

What this module deliberately does NOT do
------------------------------------------
It does not broadcast, and it does not fetch. Signing and sending are separated so
that a signed artifact can be persisted, re-decoded and compared against the
approved bundle before anything reaches a node — a signed transaction is an
irreversible capability the moment it exists, whether or not it was sent.
"""

from __future__ import annotations

import inspect
import os
import stat as stat_module
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import structlog

from scout.live.evm.signing_bundle import ExecutionSigningBundle

log = structlog.get_logger(__name__)

__all__ = [
    "EvmSignerBoundary",
    "SignerRefused",
    "SignerUnavailable",
    "CustodyProblem",
    "assess_key_custody",
    "REQUIRED_MODE",
]

#: Owner read/write only. A key any other account can read is a key that account
#: holds, whatever the process does with it afterwards.
REQUIRED_MODE = 0o600


class SignerRefused(Exception):
    """Signing was refused. The key was NOT read."""

    def __init__(self, gate: str, message: str) -> None:
        super().__init__(message)
        self.gate = gate
        self.message = message


class SignerUnavailable(Exception):
    """The signer cannot be used — missing key, bad custody, no backend.

    Separate from :class:`SignerRefused` because the operator response differs: a
    refusal means the request was wrong, unavailability means the environment is.
    """


@dataclass(frozen=True)
class CustodyProblem:
    reason: str
    detail: str


class MandateLike(Protocol):
    """The shape the boundary needs from a mandate. Structural, so a test double
    cannot accidentally satisfy it by having the right attribute names only."""

    def authorize_bundle(self, bundle: ExecutionSigningBundle) -> Any: ...


def evaluate_custody_policy(
    *,
    mode: int,
    file_uid: int | None,
    current_uid: int | None,
    path: str,
) -> CustodyProblem | None:
    """PURE policy over stat metadata. No filesystem, no platform dependence.

    Split out from :func:`assess_key_custody` for the same reason the Solana
    signer splits its policy: Windows does not honour ``chmod``, so a policy that
    could only be exercised through the filesystem would be first tested on the
    Linux host on trade day. This function is verifiable everywhere; the thin
    wrapper below is what is POSIX-only.
    """
    if not stat_module.S_ISREG(mode):
        return CustodyProblem("not_a_file", f"{path} is not a regular file")

    permission_bits = stat_module.S_IMODE(mode)
    if permission_bits & 0o077:
        return CustodyProblem(
            "permissive_mode",
            f"{path} is mode {permission_bits:04o}; group/other bits are set, so "
            f"accounts other than the owner can read it. Required "
            f"{REQUIRED_MODE:04o}.",
        )
    if current_uid is not None and file_uid is not None and file_uid != current_uid:
        return CustodyProblem(
            "wrong_owner",
            f"{path} is owned by uid {file_uid}, not the running uid {current_uid}",
        )
    return None


def assess_key_custody(path: Path) -> CustodyProblem | None:
    """Custody verdict for a key file. Stats it; does NOT open it.

    Answers the custody question completely while leaving the key unread, which
    is what lets it run before any authorization exists.

    On a platform without POSIX ownership semantics the mode bits are not
    meaningful, so the permission check is skipped rather than faked — a check
    that cannot mean anything must not pretend to. Production is Linux; the
    policy itself is tested on every platform via
    :func:`evaluate_custody_policy`.
    """
    try:
        info = path.stat()
    except FileNotFoundError:
        return CustodyProblem("missing", f"no signer key at {path}")
    except OSError as exc:
        return CustodyProblem("unstattable", f"cannot stat {path}: {exc}")

    posix = hasattr(os, "getuid")
    if not posix and stat_module.S_ISREG(info.st_mode):
        # Non-POSIX: only the "is it a file" half of the policy is meaningful.
        return None
    return evaluate_custody_policy(
        mode=info.st_mode,
        file_uid=info.st_uid if posix else None,
        current_uid=os.getuid() if posix else None,
        path=str(path),
    )


class EvmSignerBoundary:
    """Gate, then load, then sign — in that order, structurally.

    ``key_loader`` is called at most once per :meth:`sign_bundle`, and never
    before the mandate has authorized the exact bundle.
    """

    def __init__(
        self,
        *,
        mandate: MandateLike,
        key_path: Path | str | None,
        key_loader: Callable[[Path], Any],
        expected_address: str | None = None,
    ) -> None:
        self._mandate = mandate
        self._key_path = Path(key_path) if key_path else None
        self._key_loader = key_loader
        self._expected_address = expected_address.lower() if expected_address else None

    # ------------------------------------------------------------------
    def precheck_custody(self) -> None:
        """Verify the key file is safe to use WITHOUT opening it.

        Runs before any authorization exists, which is the whole reason it uses
        ``stat`` rather than a read: a refusal here happens with nothing built and
        nothing sent, and the key still untouched.
        """
        if self._key_path is None:
            raise SignerUnavailable(
                "no EVM signer key is configured (EVM_SIGNER_KEY_PATH unset); "
                "there is no identity to sign with"
            )
        problem = assess_key_custody(self._key_path)
        if problem is not None:
            raise SignerUnavailable(f"[{problem.reason}] {problem.detail}")

    # ------------------------------------------------------------------
    def sign_bundle(self, bundle: ExecutionSigningBundle) -> Any:
        """Authorize, THEN load the key, THEN sign.

        Ordering is the security property, so it is expressed as straight-line
        code with no branch that can reach the loader first.
        """
        # 1. The bundle must still be what it says it is. Everything below
        #    reasons about its terms; an unverifiable bundle makes every
        #    subsequent check meaningless.
        if not bundle.verify():
            raise SignerRefused(
                "integrity",
                "bundle fails verify(): its terms no longer match its hash. The "
                "key was not read.",
            )

        # 1b. The bundle must not already be stale. `prepare_execution` checks
        #     freshness against the chain, but `sign_bundle` accepts ANY bundle —
        #     a caller holding one from thirty minutes ago could otherwise sign
        #     it. Checked here so the guarantee does not depend on the caller
        #     having come through the orchestrator.
        now = datetime.now(timezone.utc)
        if bundle.is_expired(at=now):
            raise SignerRefused(
                "expiry",
                f"bundle {bundle.bundle_hash[:16]} expired at "
                f"{bundle.quote_expires_at.isoformat()}; re-quote. The key was "
                "not read.",
            )

        # 2. THE GATE.
        try:
            decision = self._mandate.authorize_bundle(bundle)
        except Exception as exc:
            raise SignerRefused(
                "mandate",
                f"the execution mandate refused this bundle: {exc}. The key was "
                "not read.",
            ) from exc

        # *** A COROUTINE IS NOT AN AUTHORIZATION. ***
        # An earlier version trusted "it did not raise" as approval. An ASYNC
        # `authorize_bundle` returns an un-awaited coroutine — truthy, no
        # exception — and the key was read. That is not hypothetical: the only
        # mandate in this tree, `ExecutionMandate.authorize`, IS async and awaits
        # a database read, so an async implementation is the likely one.
        #
        # A falsy return is refused for the same reason: the previous comment
        # asserted "refusal raises, so there is no falsy-return path" — an
        # assumption about a component that does not exist yet, not a property
        # this boundary enforced.
        if inspect.isawaitable(decision):
            try:  # pragma: no cover - defensive cleanup
                decision.close()  # type: ignore[union-attr]
            except Exception:
                log.exception("evm_signer_coroutine_close_failed")
            raise SignerRefused(
                "mandate",
                "authorize_bundle returned an awaitable that was never awaited, "
                "so no authorization decision was ever made. An async mandate "
                "must be awaited by its caller before reaching the signer. The "
                "key was not read.",
            )
        if not decision:
            raise SignerRefused(
                "mandate",
                f"authorize_bundle returned {decision!r} rather than a decision. "
                "A falsy result is a refusal. The key was not read.",
            )

        # 3. Custody, still without opening the file.
        self.precheck_custody()

        # 4. ONLY NOW is the key material read.
        log.info(
            "evm_signer_loading_key",
            bundle_hash=bundle.bundle_hash,
            chain_id=bundle.chain_id,
            wallet=bundle.wallet,
        )
        assert self._key_path is not None  # narrowed by precheck_custody
        account = self._key_loader(self._key_path)

        # 5. The loaded key must BE the wallet the bundle authorized. A key that
        #    signs as a different address would produce a valid signature for an
        #    account nobody approved.
        address = getattr(account, "address", None)
        if address is None:
            raise SignerUnavailable(
                "the key loader returned an object with no `address`; cannot "
                "prove which account would sign"
            )
        if address.lower() != bundle.wallet:
            raise SignerRefused(
                "wallet_mismatch",
                f"the loaded key signs as {address.lower()} but the bundle "
                f"authorizes {bundle.wallet}. Refusing.",
            )
        if self._expected_address and address.lower() != self._expected_address:
            raise SignerRefused(
                "wallet_mismatch",
                f"the loaded key signs as {address.lower()} but the configured "
                f"signer address is {self._expected_address}. Refusing.",
            )
        return SignerSession(account=account, bundle=bundle, decision=decision)


@dataclass(frozen=True)
class SignerSession:
    """A loaded key bound to the exact bundle that authorized loading it.

    Handed back instead of a raw account so a caller cannot carry the key over to
    a different bundle: whatever signs, signs for THIS one.
    """

    account: Any
    bundle: ExecutionSigningBundle
    decision: Any
