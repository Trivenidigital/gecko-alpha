"""Signer seam — the ONLY module that holds Solana private key material.

Phase 1: LocalKeypairSigner loads the key in-process from the
SOLANA_WALLET_SECRET secret (base58). NOTE: this class does NOT encrypt the
key — it holds an in-memory Keypair. "At rest" protection comes from how the
secret is supplied (Pydantic SecretStr, kept out of .env.example/git), not
from this class. A future RemoteSigner can implement the same Signer Protocol
(pubkey/sign) to move signing into an isolated service with zero adapter changes.
"""

from __future__ import annotations

import base64
from typing import Protocol, runtime_checkable

import structlog
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction

log = structlog.get_logger(__name__)


@runtime_checkable
class Signer(Protocol):
    def pubkey(self) -> str: ...
    def sign(self, tx_b64: str) -> str: ...


class LocalKeypairSigner:
    """In-process signer holding a plaintext Keypair in memory. Never logs,
    reprs, or persists key bytes. (Not encrypted — see module docstring.)"""

    def __init__(self, secret_base58: str) -> None:
        self._kp = Keypair.from_base58_string(secret_base58)
        self._pubkey = str(self._kp.pubkey())

    def pubkey(self) -> str:
        return self._pubkey

    def sign(self, tx_b64: str) -> str:
        raw = base64.b64decode(tx_b64)
        unsigned = VersionedTransaction.from_bytes(raw)
        signed = VersionedTransaction(unsigned.message, [self._kp])
        return base64.b64encode(bytes(signed)).decode()

    def __repr__(self) -> str:  # never expose key
        return f"<LocalKeypairSigner pubkey={self._pubkey}>"

    __str__ = __repr__


def make_signer(settings) -> Signer | None:
    secret = getattr(settings, "SOLANA_WALLET_SECRET", None)
    if secret is None:
        return None
    raw = (
        secret.get_secret_value()
        if hasattr(secret, "get_secret_value")
        else str(secret)
    )
    if not raw:
        return None
    return LocalKeypairSigner(raw)
