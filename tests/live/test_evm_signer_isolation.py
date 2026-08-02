"""*** AN INACTIVE MANDATE CANNOT LOAD THE EVM KEY, LET ALONE SIGN. ***

The Solana lane's guarantee is that the funded key is not READ before
authorization — not merely unused, unread — and its `loader_spy.calls == 0`
assertions are why that is believed rather than hoped. This is the same
guarantee for the EVM lane, asserted the same way.

The loader here does not merely record: it RAISES. A test that counts calls fails
on a missing assertion; a test whose loader explodes fails on the thing itself,
and cannot be satisfied by accident.
"""

from __future__ import annotations

import os
import stat as stat_module
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from scout.live.evm.signer import (
    REQUIRED_MODE,
    EvmSignerBoundary,
    SignerRefused,
    SignerUnavailable,
    assess_key_custody,
    evaluate_custody_policy,
)
from scout.live.evm.signing_bundle import ExecutionSigningBundle

_T0 = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
_WALLET = "0x28c6c06298d514db089934071355e5743bf21d60"

#: Windows does not honour `chmod`, so filesystem-mode assertions are meaningless
#: there. The POLICY is exercised on every platform through
#: `evaluate_custody_policy`; only the thin stat wrapper is POSIX-gated.
_POSIX_ONLY = pytest.mark.skipif(
    not hasattr(os, "getuid"), reason="POSIX permission semantics required"
)


class KeyWasRead(AssertionError):
    """Raised by the tripwire loader. Never caught in these tests.

    An AssertionError subclass so a broad `except Exception` in the code under
    test would still surface it as a failure rather than swallowing it.
    """


def _tripwire_loader(_path):
    raise KeyWasRead("the signer key was READ before the bundle was authorized")


class _CountingLoader:
    """Records calls and returns an account with a fixed address."""

    def __init__(self, address: str = _WALLET) -> None:
        self.calls = 0
        self._address = address

    def __call__(self, _path):
        self.calls += 1

        class _Account:
            address = self._address

        return _Account()


class _RefusingMandate:
    """The shipped posture: refuses every bundle."""

    def __init__(self, message: str = "mandate is DISABLED") -> None:
        self.message = message
        self.calls = 0

    def authorize_bundle(self, bundle):
        self.calls += 1
        raise RuntimeError(self.message)


class _PermittingMandate:
    """Returns a REAL `MandateDecision`.

    A dict used to be enough, because the boundary only checked truthiness — so
    `True`, `object()` and `MagicMock()` all authorized. Only the object the
    mandate mints on its permitted path counts now, and these doubles must mint
    one too or they are not modelling a mandate.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.seen = []

    def authorize_bundle(self, bundle):
        from decimal import Decimal

        from scout.live.mandate import MandateDecision, MandateEnvelope

        self.calls += 1
        self.seen.append(bundle.bundle_hash)
        return MandateDecision(
            mode="SUPERVISED_LIVE",
            venue="zeroex-allowance-holder",
            venue_family="dex",
            intent_hash=bundle.intent_hash,
            envelope=MandateEnvelope(
                per_trade_max_notional_usd=Decimal("500"),
                daily_max_notional_usd=Decimal("500"),
                max_open_positions=1,
            ),
            supervised_reconciled=None,
            decided_at=datetime.now(timezone.utc),
        )


def _bundle(**over) -> ExecutionSigningBundle:
    kw = dict(
        intent_hash="i" * 64,
        provider="0x",
        provider_version="v2",
        adapter_version="zeroex-adapter-v1",
        response_hash="r" * 64,
        chain_id=1,
        wallet=_WALLET,
        to="0x0000000000001ff3684f28c67538d4d072c22734",
        calldata_template_hash="c" * 64,
        value=0,
        sell_token="0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",
        buy_token="0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
        sell_amount=10**17,
        minimum_output=187310865,
        quote_block=25670103,
        # Relative to real NOW, not to the historical _T0: `sign_bundle` checks
        # expiry itself now, so a bundle pinned to a past timestamp would refuse
        # at the expiry gate before reaching whichever gate is under test.
        quote_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        gas_ceiling=233965,
        gas_price_ceiling=73673518,
    )
    kw.update(over)
    return ExecutionSigningBundle(**kw)


def _key_file(tmp_path) -> Path:
    path = tmp_path / "evm.key"
    path.write_text("0x" + "11" * 32, encoding="utf-8")
    os.chmod(path, REQUIRED_MODE)
    return path


# ===========================================================================
# The guarantee
# ===========================================================================


class TestInactiveMandateNeverReadsTheKey:
    def test_a_refusing_mandate_does_not_reach_the_loader(self, tmp_path):
        """*** THE LOAD-BEARING TEST. ***"""
        mandate = _RefusingMandate()
        boundary = EvmSignerBoundary(
            mandate=mandate,
            key_path=_key_file(tmp_path),
            key_loader=_tripwire_loader,  # explodes if ever called
        )
        with pytest.raises(SignerRefused) as exc:
            boundary.sign_bundle(_bundle())
        assert exc.value.gate == "mandate"
        assert "key was not read" in exc.value.message
        assert mandate.calls == 1, "the gate must actually have been consulted"

    def test_the_loader_is_not_called_even_once(self, tmp_path):
        """Belt to the tripwire braces: counted, not just un-exploded."""
        loader = _CountingLoader()
        boundary = EvmSignerBoundary(
            mandate=_RefusingMandate(), key_path=_key_file(tmp_path), key_loader=loader
        )
        with pytest.raises(SignerRefused):
            boundary.sign_bundle(_bundle())
        assert loader.calls == 0

    def test_a_tampered_bundle_is_refused_before_the_gate_and_the_key(self, tmp_path):
        """Integrity precedes everything: an unverifiable bundle makes every
        later check meaningless, so it must not even reach the mandate."""
        mandate = _PermittingMandate()
        bundle = _bundle()
        object.__setattr__(bundle, "minimum_output", 1)
        boundary = EvmSignerBoundary(
            mandate=mandate, key_path=_key_file(tmp_path), key_loader=_tripwire_loader
        )
        with pytest.raises(SignerRefused) as exc:
            boundary.sign_bundle(bundle)
        assert exc.value.gate == "integrity"
        assert mandate.calls == 0, "a broken bundle reached the mandate"

    @_POSIX_ONLY
    def test_custody_problems_do_not_read_the_key_either(self, tmp_path):
        """Custody is answered by `stat`, never by opening. A refusal here happens
        with the key untouched."""
        path = _key_file(tmp_path)
        os.chmod(path, 0o644)  # group/other readable
        boundary = EvmSignerBoundary(
            mandate=_PermittingMandate(), key_path=path, key_loader=_tripwire_loader
        )
        with pytest.raises(SignerUnavailable) as exc:
            boundary.sign_bundle(_bundle())
        assert "permissive_mode" in str(exc.value)

    def test_no_configured_key_is_unavailable_not_a_crash(self, tmp_path):
        boundary = EvmSignerBoundary(
            mandate=_PermittingMandate(), key_path=None, key_loader=_tripwire_loader
        )
        with pytest.raises(SignerUnavailable, match="EVM_SIGNER_KEY_PATH"):
            boundary.sign_bundle(_bundle())

    def test_a_missing_key_file_is_unavailable(self, tmp_path):
        boundary = EvmSignerBoundary(
            mandate=_PermittingMandate(),
            key_path=tmp_path / "absent.key",
            key_loader=_tripwire_loader,
        )
        with pytest.raises(SignerUnavailable, match="missing"):
            boundary.sign_bundle(_bundle())

    def test_precheck_custody_alone_never_opens_the_file(self, tmp_path):
        """The precheck runs before anything is built. It must be safe to call
        with no authorization in existence."""
        path = _key_file(tmp_path)
        boundary = EvmSignerBoundary(
            mandate=_RefusingMandate(), key_path=path, key_loader=_tripwire_loader
        )
        boundary.precheck_custody()  # must not raise, must not read


class TestTheGuardOnTheGuard:
    """Without these, every test above would pass on a boundary that can never
    sign at all."""

    def test_a_permitting_mandate_does_load_the_key(self, tmp_path):
        loader = _CountingLoader()
        mandate = _PermittingMandate()
        bundle = _bundle()
        boundary = EvmSignerBoundary(
            mandate=mandate, key_path=_key_file(tmp_path), key_loader=loader
        )
        session = boundary.sign_bundle(bundle)
        assert loader.calls == 1
        assert mandate.seen == [bundle.bundle_hash]
        # The key comes back BOUND to the bundle that authorized loading it, so
        # it cannot be carried over to a different one.
        assert session.bundle is bundle

    def test_the_tripwire_loader_actually_trips(self):
        with pytest.raises(KeyWasRead):
            _tripwire_loader(Path("/nowhere"))

    def test_a_key_for_a_different_wallet_is_refused(self, tmp_path):
        """A valid signature for an account nobody approved is still the wrong
        signature."""
        loader = _CountingLoader(address="0x" + "de" * 20)
        boundary = EvmSignerBoundary(
            mandate=_PermittingMandate(),
            key_path=_key_file(tmp_path),
            key_loader=loader,
        )
        with pytest.raises(SignerRefused) as exc:
            boundary.sign_bundle(_bundle())
        assert exc.value.gate == "wallet_mismatch"

    def test_a_loader_returning_no_address_is_unavailable(self, tmp_path):
        def _opaque(_path):
            return object()

        boundary = EvmSignerBoundary(
            mandate=_PermittingMandate(),
            key_path=_key_file(tmp_path),
            key_loader=_opaque,
        )
        with pytest.raises(SignerUnavailable, match="no `address`"):
            boundary.sign_bundle(_bundle())


class TestCustodyPolicy:
    """The policy itself — pure, and therefore verifiable on every platform."""

    def _mode(self, bits: int) -> int:
        return stat_module.S_IFREG | bits

    def test_owner_only_mode_owned_by_us_is_accepted(self):
        assert (
            evaluate_custody_policy(
                mode=self._mode(0o600), file_uid=1000, current_uid=1000, path="/k/evm"
            )
            is None
        )

    @pytest.mark.parametrize("bits", [0o640, 0o604, 0o666, 0o777, 0o660, 0o606])
    def test_any_group_or_other_bit_is_refused(self, bits):
        problem = evaluate_custody_policy(
            mode=self._mode(bits), file_uid=1000, current_uid=1000, path="/k/evm"
        )
        assert problem is not None and problem.reason == "permissive_mode"

    def test_a_key_owned_by_another_account_is_refused(self):
        problem = evaluate_custody_policy(
            mode=self._mode(0o600), file_uid=0, current_uid=1000, path="/k/evm"
        )
        assert problem is not None and problem.reason == "wrong_owner"

    def test_a_directory_is_refused(self):
        problem = evaluate_custody_policy(
            mode=stat_module.S_IFDIR | 0o700,
            file_uid=1000,
            current_uid=1000,
            path="/k",
        )
        assert problem is not None and problem.reason == "not_a_file"

    def test_the_verdict_never_leaks_key_contents(self):
        """A custody error is printed and logged; it must describe metadata only."""
        problem = evaluate_custody_policy(
            mode=self._mode(0o644), file_uid=1000, current_uid=1000, path="/k/evm"
        )
        assert problem is not None
        assert "0x" not in problem.detail

    def test_the_required_mode_is_owner_only(self):
        assert stat_module.S_IMODE(REQUIRED_MODE) == 0o600
        assert REQUIRED_MODE & 0o077 == 0

    @_POSIX_ONLY
    def test_the_stat_wrapper_agrees_with_the_policy(self, tmp_path):
        """Guard on the split: the wrapper must actually apply the policy, not
        merely exist beside it."""
        path = _key_file(tmp_path)
        assert assess_key_custody(path) is None
        os.chmod(path, 0o644)
        problem = assess_key_custody(path)
        assert problem is not None and problem.reason == "permissive_mode"

    def test_a_missing_file_is_reported_by_the_wrapper(self, tmp_path):
        problem = assess_key_custody(tmp_path / "absent.key")
        assert problem is not None and problem.reason == "missing"
