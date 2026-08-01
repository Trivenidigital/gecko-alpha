"""PR-S1: Solana lane config defaults and validators.

The defaults matter as much as the validators: an operator who sets nothing
must end up with a lane that reads mainnet and refuses to trade, never one
that is one env var away from signing.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scout.config import Settings


def test_defaults_are_the_refusing_ones(settings_factory):
    s = settings_factory()

    # No key configured => signer refuses; the runner cannot trade by accident.
    assert s.SOLANA_PILOT_KEYPAIR_PATH == ""
    assert s.SOLANA_RPC_URL == "https://api.mainnet-beta.solana.com"
    assert s.JUPITER_API_BASE == "https://api.jup.ag/swap/v1"
    assert s.JITO_BLOCK_ENGINE_URL == "https://mainnet.block-engine.jito.wtf"
    assert s.JUPITER_API_KEY is None
    assert s.SOLANA_PILOT_SLIPPAGE_BPS == 100
    assert s.SOLANA_PILOT_MIN_ORDER_USD == 5.0
    assert s.SOLANA_PILOT_MAX_ORDER_USD == 10.0
    assert s.SOLANA_HTTP_TIMEOUT_SEC > 0


def test_default_fee_ceilings_are_internally_consistent(settings_factory):
    """The shipped defaults must not themselves be an unsignable combination."""
    s = settings_factory()
    floor = (
        s.SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS
        + s.SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS
        + 5_000
    )
    assert s.SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS >= floor


def test_inverted_order_bounds_rejected(settings_factory):
    with pytest.raises(ValidationError, match="SOLANA_PILOT_MIN_ORDER_USD must be <="):
        settings_factory(
            SOLANA_PILOT_MIN_ORDER_USD=25.0, SOLANA_PILOT_MAX_ORDER_USD=10.0
        )


@pytest.mark.parametrize("bps", [0, -1, 10_001])
def test_out_of_range_slippage_rejected(settings_factory, bps):
    with pytest.raises(ValidationError, match="SOLANA_PILOT_SLIPPAGE_BPS"):
        settings_factory(SOLANA_PILOT_SLIPPAGE_BPS=bps)


def test_total_fee_ceiling_below_components_rejected(settings_factory):
    """Otherwise every build is unsignable and the lane silently never trades."""
    with pytest.raises(ValidationError, match="SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS"):
        settings_factory(
            SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS=1_000_000,
            SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=1_000_000,
            SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS=500_000,
        )


def test_negative_fee_ceiling_rejected(settings_factory):
    with pytest.raises(ValidationError, match="must be >= 0"):
        settings_factory(SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=-1)


def test_coherent_overrides_accepted(settings_factory):
    """A total exactly on the floor is accepted; below it is not.

    The floor is priority + tip + base signature fee + ATA rent, and the rent
    term is why ``SOLANA_PILOT_MAX_ATA_CREATES=0`` is set here: this case is
    about the FEE arithmetic, and leaving the default of 3 would reserve
    6,117,840 lamports of rent room that a build with no ATA create cannot
    spend.
    """
    s = settings_factory(
        SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS=200_000,
        SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=300_000,
        SOLANA_PILOT_MAX_ATA_CREATES=0,
        SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS=505_000,
        SOLANA_PILOT_SLIPPAGE_BPS=50,
    )
    assert s.SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS == 505_000
    assert s.SOLANA_PILOT_SLIPPAGE_BPS == 50

    # One lamport of rent room, and the same total no longer reaches the floor.
    with pytest.raises(ValidationError, match="ATA rent"):
        settings_factory(
            SOLANA_PILOT_MAX_PRIORITY_FEE_LAMPORTS=200_000,
            SOLANA_PILOT_MAX_JITO_TIP_LAMPORTS=300_000,
            SOLANA_PILOT_MAX_ATA_CREATES=1,
            SOLANA_PILOT_MAX_TOTAL_FEE_LAMPORTS=505_000,
        )


def test_unknown_solana_key_is_rejected(settings_factory):
    """extra='forbid' — a typo'd ceiling must not silently do nothing."""
    with pytest.raises(ValidationError):
        settings_factory(SOLANA_PILOT_MAX_TIP_LAMPORTS=1)


def test_api_key_is_secret(settings_factory):
    """The Jupiter key must not render in logs or evidence dumps."""
    s = settings_factory(JUPITER_API_KEY="super-secret")
    assert "super-secret" not in repr(s.JUPITER_API_KEY)
    assert "super-secret" not in str(s.JUPITER_API_KEY)
    assert s.JUPITER_API_KEY.get_secret_value() == "super-secret"


def test_settings_loads_without_any_solana_env(monkeypatch):
    """A box that has never heard of the Solana lane must still boot."""
    for key in list(Settings.model_fields):
        if key.startswith(("SOLANA_", "JUPITER_", "JITO_")):
            monkeypatch.delenv(key, raising=False)
    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    assert s.SOLANA_PILOT_KEYPAIR_PATH == ""
