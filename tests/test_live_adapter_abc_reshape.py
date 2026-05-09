"""BL-NEW-LIVE-HYBRID M1: ExchangeAdapter ABC reshape."""

from __future__ import annotations

import inspect
from pathlib import Path

# Read source as text — avoids Windows OpenSSL Applink crash from
# importing scout.live.adapter_base (which transitively pulls aiohttp).
_ADAPTER_BASE = (
    Path(__file__).resolve().parent.parent / "scout" / "live" / "adapter_base.py"
).read_text(encoding="utf-8")


def test_abc_has_place_order_request_method():
    assert "def place_order_request" in _ADAPTER_BASE


def test_abc_has_await_fill_confirmation_method():
    assert "def await_fill_confirmation" in _ADAPTER_BASE


def test_abc_has_fetch_venue_metadata_method():
    assert "def fetch_venue_metadata" in _ADAPTER_BASE


def test_abc_has_fetch_account_balance_method():
    assert "def fetch_account_balance" in _ADAPTER_BASE


def test_abc_defines_venue_metadata_dataclass():
    assert "class VenueMetadata" in _ADAPTER_BASE
    assert "@dataclass" in _ADAPTER_BASE


def test_abc_defines_order_request_dataclass():
    assert "class OrderRequest" in _ADAPTER_BASE


def test_abc_defines_order_confirmation_dataclass():
    assert "class OrderConfirmation" in _ADAPTER_BASE


def test_abc_keeps_existing_back_compat_methods():
    """Build-stage drift-check (2026-05-08): existing methods MUST stay
    on the ABC because 6 dependent modules use them. ADDITIVE reshape."""
    for old_method in [
        "fetch_exchange_info_row",
        "resolve_pair_for_symbol",
        "fetch_depth",
        "fetch_price",
        "send_order",
    ]:
        assert (
            f"def {old_method}" in _ADAPTER_BASE
        ), f"{old_method} removed; would break dependent modules"
