"""Local ed25519 signing for the Solana lane (PR-S1).

The private key never leaves this process. It is read from disk at CALL time,
used, and dropped — never cached in a module global, never written anywhere,
never passed through argv or the environment, and never logged. Only the
public key and the resulting signature (both public by construction) appear in
log events.

Verified fact (2026-07-31): for a v0 transaction the signed payload is the
VERSION-PREFIXED message — ``0x80 || bytes(message)``, which
``solders.message.to_bytes_versioned`` produces. Verified empirically against
solders 0.28.0: signing that payload reproduces byte-for-byte the signature
``VersionedTransaction(msg, [kp])`` generates, and the resulting populated
transaction serialises identically. Signing the UNPREFIXED ``bytes(message)``
yields a DIFFERENT signature that fails verification — a trap worth naming,
because it produces a well-formed transaction that simply never lands.

That fact is what makes the expected signature derivable LOCALLY and
therefore knowable BEFORE submission. The signature is a pure function of the
message and the key; nothing about it depends on the block engine having seen
it. Persisting it pre-submission is what turns an ambiguous submission from
"did anything happen?" into a resolvable question.

Key-file format: a Solana CLI ``id.json`` — a JSON array of 64 integers, the
ed25519 secret key followed by the public key.
"""

from __future__ import annotations

import json
import os
import stat as stat_module
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

from scout.config import Settings
from scout.live.solana.exceptions import SolanaKeypairError

log = structlog.get_logger(__name__)

# Required key-file mode: owner read/write only.
REQUIRED_MODE = 0o600
_KEYPAIR_BYTES = 64


@dataclass(frozen=True)
class SignedTransaction:
    """A locally-signed transaction and the signature derived for it.

    ``signature`` is the base58 string the cluster will know this transaction
    by. It is computed here, before any network call, and MUST be persisted
    before submission — see the module docstring.
    """

    signed_tx_b64: str
    signature: str
    message_sha256: str
    signer_pubkey: str
    signed_tx_bytes: bytes = field(repr=False, default=b"")


def enforce_keyfile_security(
    *, mode: int, file_uid: int, current_uid: int, path: str
) -> None:
    """Pure policy check for a key file's stat metadata.

    Split from the filesystem so the policy itself is testable without
    depending on the host OS honouring ``chmod`` — Windows does not, and a
    policy that can only be exercised on the deployment box is a policy that
    gets verified for the first time on pilot day.

    Args:
        mode: ``st_mode``; only the permission bits are examined.
        file_uid: ``st_uid`` of the key file.
        current_uid: uid of the running process.

    Raises:
        SolanaKeypairError: permissions wider than 0600, or another owner.
    """
    permission_bits = stat_module.S_IMODE(mode)
    if permission_bits != REQUIRED_MODE:
        raise SolanaKeypairError(
            f"keypair file {path} has mode {permission_bits:#o}; "
            f"required {REQUIRED_MODE:#o} (owner read/write only). "
            "A key readable by group or other is a key that must be rotated, "
            "not chmod'ed and reused."
        )
    if file_uid != current_uid:
        raise SolanaKeypairError(
            f"keypair file {path} is owned by uid {file_uid} but this process "
            f"runs as uid {current_uid}. Refusing to load another user's key."
        )


def load_keypair(settings: Settings, *, path: str | None = None) -> Keypair:
    """Load the pilot signing keypair from disk at call time.

    Args:
        path: Overrides ``SOLANA_PILOT_KEYPAIR_PATH``. Present for tests and
            for a runner that resolves the path itself; the key material is
            never a parameter.

    Raises:
        SolanaKeypairError: unset path, missing file, unsafe permissions,
            wrong owner, or a malformed id.json. The exception message never
            contains key material or file contents.
    """
    keypair_path = path if path is not None else settings.SOLANA_PILOT_KEYPAIR_PATH
    if not keypair_path:
        raise SolanaKeypairError(
            "SOLANA_PILOT_KEYPAIR_PATH is unset. The Solana pilot refuses to "
            "run without an explicitly configured key file."
        )

    resolved = Path(keypair_path)
    if not resolved.is_file():
        raise SolanaKeypairError(f"keypair file not found: {resolved}")

    if hasattr(os, "getuid"):
        info = resolved.stat()
        enforce_keyfile_security(
            mode=info.st_mode,
            file_uid=info.st_uid,
            current_uid=os.getuid(),
            path=str(resolved),
        )
    else:
        # Windows reports synthetic POSIX mode bits and no meaningful uid, so
        # the check cannot be performed rather than performed and passed.
        # Refusing is the fail-closed reading: the pilot host is Linux, and a
        # silent pass here would mean the guarantee is absent exactly where
        # nobody looks for it.
        raise SolanaKeypairError(
            "refusing to load a signing key on a non-POSIX platform: key-file "
            "permissions and ownership cannot be verified. Run the Solana "
            "pilot on the Linux host."
        )

    try:
        payload = json.loads(resolved.read_text())
    except (OSError, ValueError) as exc:
        # Deliberately does NOT chain the original message: a JSONDecodeError
        # renders the offending document fragment, which here is key bytes.
        raise SolanaKeypairError(
            f"keypair file {resolved} is not readable JSON ({type(exc).__name__})"
        ) from None

    if not isinstance(payload, list) or len(payload) != _KEYPAIR_BYTES:
        raise SolanaKeypairError(
            f"keypair file {resolved} must contain a JSON array of "
            f"{_KEYPAIR_BYTES} integers; got "
            f"{type(payload).__name__} of length "
            f"{len(payload) if isinstance(payload, list) else 'n/a'}"
        )

    try:
        keypair = Keypair.from_bytes(bytes(payload))
    except (ValueError, TypeError, OverflowError) as exc:
        raise SolanaKeypairError(
            f"keypair file {resolved} does not decode to a valid ed25519 "
            f"keypair ({type(exc).__name__})"
        ) from None

    log.info("solana_keypair_loaded", pubkey=str(keypair.pubkey()), path=str(resolved))
    return keypair


def sign_transaction(
    tx_b64: str, keypair: Keypair, *, expected_signer: str | None = None
) -> SignedTransaction:
    """Sign a base64 transaction locally and derive its signature.

    The signature returned is THE signature the cluster will index this
    transaction by, derived here before any network call. Persist it before
    submitting — ``resolver.resolve_submission`` needs it to answer what
    happened when a submission's outcome is unknown.

    Args:
        expected_signer: if given, the keypair's pubkey must equal it. Cheap
            guard against signing with a key that is not the one the
            transaction was BUILT for, which would otherwise surface as an
            opaque on-chain signature-verification failure.

    Raises:
        SolanaKeypairError: keypair does not match ``expected_signer``, or the
            transaction cannot be deserialised.
    """
    import base64

    pubkey = str(keypair.pubkey())
    if expected_signer is not None and pubkey != expected_signer:
        raise SolanaKeypairError(
            f"loaded keypair {pubkey} does not match the expected signer "
            f"{expected_signer}; refusing to sign"
        )

    try:
        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64, validate=True))
    except Exception as exc:
        raise SolanaKeypairError(
            f"transaction could not be deserialised for signing "
            f"({type(exc).__name__})"
        ) from None

    message = tx.message
    payload = to_bytes_versioned(message)
    signature = keypair.sign_message(payload)

    signed = VersionedTransaction.populate(message, [signature])
    signed_bytes = bytes(signed)

    import hashlib

    result = SignedTransaction(
        signed_tx_b64=base64.b64encode(signed_bytes).decode(),
        signature=str(signature),
        message_sha256=hashlib.sha256(payload).hexdigest(),
        signer_pubkey=pubkey,
        signed_tx_bytes=signed_bytes,
    )
    log.info(
        "solana_tx_signed",
        signer_pubkey=pubkey,
        signature=result.signature,
        message_sha256=result.message_sha256,
    )
    return result
