"""PR-S1: signer — key-file security policy and local signature derivation.

The signature-derivation tests are the load-bearing ones: the lane persists an
expected signature BEFORE submitting, and if that derivation were wrong the
resolver would spend every ambiguous submission looking up a signature that
never existed and concluding "not submitted" about a transaction that landed.
"""

from __future__ import annotations

import base64
import json
import os
import stat

import pytest
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.transaction import VersionedTransaction

from scout.live.solana.exceptions import SolanaKeypairError
from scout.live.solana.signer import (
    REQUIRED_MODE,
    enforce_keyfile_security,
    load_keypair,
    sign_transaction,
)
from solana_tx_builder import OTHER, PAYER, PAYER_PUBKEY, build_swap_tx

_POSIX_ONLY = pytest.mark.skipif(
    not hasattr(os, "getuid"), reason="POSIX permission semantics required"
)


# ----------------------------------------------------------------------
# Key-file security policy. Tested as a pure function so the policy is
# verified on every platform, not only on the Linux pilot host — Windows
# does not honour chmod, and a policy first exercised on trade day is a
# policy nobody has actually tested.
# ----------------------------------------------------------------------
def test_policy_accepts_0600_owned_by_current_user():
    enforce_keyfile_security(
        mode=stat.S_IFREG | 0o600, file_uid=1000, current_uid=1000, path="/k/id.json"
    )


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660, 0o700, 0o777, 0o666])
def test_policy_rejects_wider_than_0600(mode):
    with pytest.raises(SolanaKeypairError, match="mode"):
        enforce_keyfile_security(
            mode=stat.S_IFREG | mode,
            file_uid=1000,
            current_uid=1000,
            path="/k/id.json",
        )


def test_policy_rejects_foreign_owner():
    with pytest.raises(SolanaKeypairError, match="owned by uid"):
        enforce_keyfile_security(
            mode=stat.S_IFREG | REQUIRED_MODE,
            file_uid=0,
            current_uid=1000,
            path="/k/id.json",
        )


def test_policy_error_never_leaks_contents():
    """Messages name the path and the mode, never key material."""
    with pytest.raises(SolanaKeypairError) as excinfo:
        enforce_keyfile_security(
            mode=stat.S_IFREG | 0o644,
            file_uid=1000,
            current_uid=1000,
            path="/k/id.json",
        )
    assert "/k/id.json" in str(excinfo.value)
    assert "0o644" in str(excinfo.value)


# ----------------------------------------------------------------------
# load_keypair
# ----------------------------------------------------------------------
def _write_keyfile(tmp_path, keypair: Keypair, name="id.json"):
    path = tmp_path / name
    path.write_text(json.dumps(list(bytes(keypair))))
    if hasattr(os, "chmod"):
        os.chmod(path, REQUIRED_MODE)
    return path


def test_load_keypair_requires_configured_path(settings_factory):
    with pytest.raises(SolanaKeypairError, match="SOLANA_PILOT_KEYPAIR_PATH is unset"):
        load_keypair(settings_factory())


def test_load_keypair_missing_file(settings_factory, tmp_path):
    with pytest.raises(SolanaKeypairError, match="not found"):
        load_keypair(settings_factory(), path=str(tmp_path / "absent.json"))


@_POSIX_ONLY
def test_load_keypair_refuses_world_readable_file(settings_factory, tmp_path):
    path = _write_keyfile(tmp_path, PAYER)
    os.chmod(path, 0o644)
    with pytest.raises(SolanaKeypairError, match="mode"):
        load_keypair(settings_factory(), path=str(path))


@_POSIX_ONLY
def test_load_keypair_accepts_0600(settings_factory, tmp_path):
    path = _write_keyfile(tmp_path, PAYER)
    loaded = load_keypair(settings_factory(), path=str(path))
    assert str(loaded.pubkey()) == PAYER_PUBKEY


@_POSIX_ONLY
def test_load_keypair_rejects_malformed_json(settings_factory, tmp_path):
    path = tmp_path / "id.json"
    path.write_text("[1, 2, 3]")
    os.chmod(path, REQUIRED_MODE)
    with pytest.raises(SolanaKeypairError, match="64 integers"):
        load_keypair(settings_factory(), path=str(path))


@pytest.mark.skipif(
    hasattr(os, "getuid"), reason="non-POSIX refusal path only reachable on Windows"
)
def test_load_keypair_refuses_on_non_posix(settings_factory, tmp_path):
    """The permission guarantee cannot be checked here, so loading is refused.

    Fail-closed: a silent pass would mean the guarantee is absent exactly
    where nobody thinks to look for it.
    """
    path = _write_keyfile(tmp_path, PAYER)
    with pytest.raises(SolanaKeypairError, match="non-POSIX"):
        load_keypair(settings_factory(), path=str(path))


# ----------------------------------------------------------------------
# Signing
# ----------------------------------------------------------------------
def test_derived_signature_matches_solders_own_signing():
    """THE load-bearing test of the whole lane.

    Our expected signature (derived from the version-prefixed message) must
    equal what solders produces when it signs the transaction itself. If these
    diverge, every ambiguous submission becomes unresolvable.
    """
    built = build_swap_tx()
    signed = sign_transaction(built.tx_b64, PAYER, expected_signer=PAYER_PUBKEY)

    reference = VersionedTransaction(built.transaction.message, [PAYER])
    assert signed.signature == str(reference.signatures[0])
    assert base64.b64decode(signed.signed_tx_b64) == bytes(reference)


def test_signature_is_over_the_version_prefixed_message():
    """Signing the UNPREFIXED message is the trap; pin the correct payload."""
    built = build_swap_tx()
    message = built.transaction.message
    signed = sign_transaction(built.tx_b64, PAYER)

    correct = PAYER.sign_message(to_bytes_versioned(message))
    wrong = PAYER.sign_message(bytes(message))

    assert signed.signature == str(correct)
    assert signed.signature != str(wrong)
    assert to_bytes_versioned(message) == b"\x80" + bytes(message)


def test_signed_transaction_verifies():
    built = build_swap_tx()
    signed = sign_transaction(built.tx_b64, PAYER)
    restored = VersionedTransaction.from_bytes(base64.b64decode(signed.signed_tx_b64))
    assert all(restored.verify_with_results())


def test_signature_is_derivable_before_submission_and_is_stable():
    """Deterministic ed25519: the same key + message always give the same sig.

    This is what licenses persisting the signature pre-submission.
    """
    built = build_swap_tx()
    first = sign_transaction(built.tx_b64, PAYER)
    second = sign_transaction(built.tx_b64, PAYER)
    assert first.signature == second.signature
    assert first.message_sha256 == second.message_sha256


def test_sign_refuses_mismatched_expected_signer():
    built = build_swap_tx()
    with pytest.raises(SolanaKeypairError, match="does not match the expected signer"):
        sign_transaction(built.tx_b64, OTHER, expected_signer=PAYER_PUBKEY)


def test_sign_refuses_undeserialisable_transaction():
    with pytest.raises(SolanaKeypairError, match="could not be deserialised"):
        sign_transaction("!!!not base64!!!", PAYER)


def test_signed_transaction_repr_hides_raw_bytes():
    """Evidence records get repr'd; raw bytes must not ride along."""
    built = build_swap_tx()
    signed = sign_transaction(built.tx_b64, PAYER)
    assert "signed_tx_bytes" not in repr(signed)
