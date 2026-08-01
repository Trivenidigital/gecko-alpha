"""The autonomous closer is off by default, and Kraken's autonomous exit stub
stays a refusal.

Two independent guarantees:

1. ``LIVE_CLOSER_ENABLED`` defaults False. Autonomous selling must be an act of
   configuration, never something obtained by omitting a key from ``.env``.
2. ``KrakenSpotAdapter.place_exit_order`` remains ``NotImplementedError``.
   ``live_evaluator`` already calls that method, so implementing it is what
   would arm autonomous Kraken selling. The supervised exit path must be a
   SEPARATE operator-invoked command, never this seam.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from scout.config import Settings
from scout.live.kraken_adapter import KrakenSpotAdapter


def _settings(**overrides):
    base = dict(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_closer_is_disabled_unless_explicitly_enabled():
    assert _settings().LIVE_CLOSER_ENABLED is False


def test_closer_can_still_be_turned_on_explicitly():
    assert _settings(LIVE_CLOSER_ENABLED=True).LIVE_CLOSER_ENABLED is True


def test_main_only_spawns_the_closer_loop_when_explicitly_enabled():
    """Guard the spawn condition itself, not just the flag's value."""
    import scout.main as main_mod

    src = inspect.getsource(main_mod)
    assert 'live_config.mode == "live"' in src
    assert "LIVE_CLOSER_ENABLED" in src
    # The spawn must be conditioned on the flag, not merely mention it.
    idx = src.index("live_evaluator_loop(")
    window = src[max(0, idx - 800) : idx]
    assert "LIVE_CLOSER_ENABLED" in window, (
        "live_evaluator_loop is spawned without a nearby LIVE_CLOSER_ENABLED "
        "guard — autonomous selling could start unconditionally"
    )


async def test_kraken_place_exit_order_is_still_a_refusal_stub():
    """If this ever returns instead of raising, autonomous Kraken selling is
    armed via live_evaluator. The supervised exit must not be wired here."""
    adapter = KrakenSpotAdapter(_settings())
    try:
        with pytest.raises(NotImplementedError):
            await adapter.place_exit_order(
                pair="XXBTZUSD",
                base_qty=Decimal("0.0002"),
                client_order_id="gecko-x-1",
                timeout_sec=30.0,
            )
    finally:
        await adapter.close()


def test_kraken_exit_stub_body_has_not_been_implemented():
    """A structural check that survives someone catching the exception.

    Uses the AST rather than substring matching: the stub's own message names
    the supervised alternative, so a naive grep matches its documentation and
    fails on a correct stub.
    """
    import ast
    import textwrap

    src = textwrap.dedent(inspect.getsource(KrakenSpotAdapter.place_exit_order))
    tree = ast.parse(src)
    fn = tree.body[0]

    # Exactly one statement, and it raises.
    body = [n for n in fn.body if not isinstance(n, ast.Expr)]  # drop docstring
    assert len(body) == 1, "place_exit_order should be a single raise statement"
    assert isinstance(body[0], ast.Raise), "place_exit_order must still raise"

    # No await anywhere — a refusal stub never touches the venue.
    assert not [n for n in ast.walk(fn) if isinstance(n, ast.Await)], (
        "place_exit_order contains an await — it is no longer a refusal stub, "
        "which arms the autonomous exit path in live_evaluator"
    )

    # The only call is the exception construction itself.
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    assert len(calls) == 1 and getattr(calls[0].func, "id", None) == (
        "NotImplementedError"
    ), "place_exit_order should construct nothing but its refusal exception"
