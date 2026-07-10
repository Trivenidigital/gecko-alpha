from __future__ import annotations

import base64

import pytest
from solders.hash import Hash
from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import MessageV0
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import VersionedTransaction

from scout.live.solana.wallet import LocalKeypairSigner, Signer


def _unsigned_tx_b64(payer: Keypair) -> str:
    # Minimal well-formed versioned tx (one no-op ix), blockhash is a Hash
    # (NOT bytes), default signature slot so the adapter can sign it. Verified
    # against solders 0.27 API: MessageV0.try_compile + VersionedTransaction.populate.
    ix = Instruction(Pubkey.default(), bytes([1]), [])
    msg = MessageV0.try_compile(payer.pubkey(), [ix], [], Hash.default())
    tx = VersionedTransaction.populate(msg, [Signature.default()])
    return base64.b64encode(bytes(tx)).decode()


def test_pubkey_matches_keypair():
    kp = Keypair()
    signer = LocalKeypairSigner(str(kp))
    assert signer.pubkey() == str(kp.pubkey())


def test_repr_does_not_leak_secret():
    kp = Keypair()
    secret = str(kp)
    signer = LocalKeypairSigner(secret)
    assert secret not in repr(signer)
    assert secret not in str(signer)


def test_sign_returns_base64_signed_tx():
    kp = Keypair()
    signer = LocalKeypairSigner(str(kp))
    signed_b64 = signer.sign(_unsigned_tx_b64(kp))
    raw = base64.b64decode(signed_b64)
    signed = VersionedTransaction.from_bytes(raw)
    # signature slot populated (not all-zero)
    assert any(bytes(sig) != bytes(64) for sig in signed.signatures)


def test_protocol_is_satisfied():
    assert isinstance(LocalKeypairSigner(str(Keypair())), Signer)
