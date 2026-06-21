from __future__ import annotations

import pytest

from scout.config import Settings
from scout.live.solana_swap_adapter import SolanaSwapAdapter

_REQUIRED = dict(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")


def _settings(**o):
    return Settings(_env_file=None, **_REQUIRED, **o)


class _SeqRpc:
    """Returns a queued sequence of confirm states."""

    def __init__(self, states):
        self._states = list(states)

    async def confirm_signature(self, signature):
        return self._states.pop(0) if self._states else "pending"


async def _noop_sleep(_):
    return None


def _adapter(rpc):
    a = SolanaSwapAdapter(settings=_settings(), jupiter=None, rpc=rpc, signer=None)
    a._pending["SIG"] = {"out_amount": 1_000_000, "size_usd": 10.0, "side": "buy"}
    return a


@pytest.mark.asyncio
async def test_await_filled_after_pending():
    a = _adapter(_SeqRpc(["pending", "success"]))
    conf = await a.await_fill_confirmation(
        venue_order_id="SIG", client_order_id="cid", timeout_sec=10,
        poll_interval_sec=0.5, _sleep=_noop_sleep,
    )
    assert conf.status == "filled"
    assert conf.venue_order_id == "SIG"
    assert conf.filled_qty == 1_000_000
    assert conf.fill_price is not None


@pytest.mark.asyncio
async def test_await_rejected_on_chain_failure():
    a = _adapter(_SeqRpc(["failed"]))
    conf = await a.await_fill_confirmation(
        venue_order_id="SIG", client_order_id="cid", timeout_sec=10,
        poll_interval_sec=0.5, _sleep=_noop_sleep,
    )
    assert conf.status == "rejected"


@pytest.mark.asyncio
async def test_await_timeout_when_never_lands():
    a = _adapter(_SeqRpc(["pending", "pending"]))
    conf = await a.await_fill_confirmation(
        venue_order_id="SIG", client_order_id="cid", timeout_sec=1,
        poll_interval_sec=0.5, _sleep=_noop_sleep,
    )
    assert conf.status == "timeout"
