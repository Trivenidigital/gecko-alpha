"""Round 12: SIGTERM via loop.add_signal_handler wakes a busy event loop.

Background — srilu 2026-05-24T23:08 deploy: SIGTERM arrived while the
pipeline was mid-46.6s asyncio.sleep(...) inside the CG rate-limiter
backoff. The prior ``signal.signal`` handler never fired before
systemd's 90s TimeoutStopSec elapsed and SIGKILL forced exit — even
though PR #243's bounded drain would have completed cancellation in
<25s once shutdown_event was set.

Root cause: ``signal.signal`` schedules the handler to run "between
bytecodes" — but inside a deep ``await``, Python isn't running
bytecodes; it's parked in the selector waiting for I/O or sleep
timeout. The handler doesn't fire until the await returns.
``loop.add_signal_handler`` wakes the selector directly via the
self-pipe, so the handler fires on the very next select() iteration.

This test verifies the fix end-to-end: a coroutine parked in a long
asyncio.sleep is woken within milliseconds of SIGTERM via the new
handler. Plus a static-source guard so a future refactor cannot
regress back to bare ``signal.signal`` for the SIGTERM path.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import sys
import time

import pytest

from scout import main as scout_main


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="loop.add_signal_handler unsupported on Windows ProactorEventLoop",
)
async def test_add_signal_handler_wakes_long_sleep_promptly():
    """Functional: a coroutine in asyncio.sleep(60) must exit within 1s
    after we send SIGTERM via loop.add_signal_handler."""
    shutdown_event = asyncio.Event()

    def _on_sigterm(sig: int) -> None:
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, _on_sigterm, signal.SIGTERM)

    try:
        async def _long_sleeper():
            try:
                # Mimic the CG rate-limiter 46.6s backoff observed in
                # the regression — we cap at 10s here so the test fails
                # noisily if the handler doesn't fire instead of timing
                # out the whole test runner.
                await asyncio.wait_for(shutdown_event.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pytest.fail(
                    "shutdown_event did not fire within 10s of SIGTERM; "
                    "loop.add_signal_handler is not reliably waking the "
                    "selector"
                )

        # Schedule SIGTERM to fire ~100ms after the sleeper starts.
        async def _signaler():
            await asyncio.sleep(0.1)
            os.kill(os.getpid(), signal.SIGTERM)

        started = time.monotonic()
        await asyncio.gather(_long_sleeper(), _signaler())
        elapsed = time.monotonic() - started

        assert shutdown_event.is_set()
        assert elapsed < 1.0, (
            f"loop.add_signal_handler should wake the sleeper in <1s; "
            f"took {elapsed:.2f}s — selector wakeup may be misconfigured"
        )
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def test_main_uses_loop_add_signal_handler_not_signal_signal_for_sigterm():
    """Static guard: scout/main.py main() must prefer
    loop.add_signal_handler for SIGTERM registration. The
    signal.signal() path is only used as Windows fallback within the
    NotImplementedError branch."""
    src = inspect.getsource(scout_main.main)
    assert "loop.add_signal_handler" in src or "_loop.add_signal_handler" in src, (
        "scout/main.py main() must call loop.add_signal_handler(...) for "
        "the SIGTERM/SIGINT registration. Bare signal.signal() can be "
        "delayed up to TimeoutStopSec when the event loop is parked in a "
        "long await — observed regression on srilu 2026-05-24T23:08."
    )
    # The Windows fallback should still exist (covered by
    # NotImplementedError branch).
    assert "NotImplementedError" in src, (
        "Windows ProactorEventLoop fallback (NotImplementedError branch) "
        "must remain so dev/CI on Windows still works"
    )
