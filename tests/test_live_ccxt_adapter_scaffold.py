"""BL-NEW-LIVE-HYBRID M1: CCXTAdapter scaffold."""

from __future__ import annotations

import inspect
from pathlib import Path

_CCXT_ADAPTER_PATH = (
    Path(__file__).resolve().parent.parent / "scout" / "live" / "ccxt_adapter.py"
)


def test_module_exists():
    assert _CCXT_ADAPTER_PATH.exists()


def test_class_has_required_methods():
    src = _CCXT_ADAPTER_PATH.read_text(encoding="utf-8")
    assert "class CCXTAdapter" in src
    for method in [
        "fetch_venue_metadata",
        "place_order_request",
        "await_fill_confirmation",
        "fetch_depth",
        "fetch_account_balance",
    ]:
        assert f"async def {method}" in src, f"missing: {method}"


def test_class_takes_venue_name_kwarg():
    src = _CCXT_ADAPTER_PATH.read_text(encoding="utf-8")
    assert "venue_name" in src and "ccxt." in src
