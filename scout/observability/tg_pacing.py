"""Per-chat Telegram pacing state (P1 #2).

Measurement lives in ``tg_dispatch_counter`` ("does NOT rate-limit or pace"); THIS
module records ``retry_after`` deadlines so ``scout.alerter`` can wait before
re-hitting a chat Telegram just 429'd. ``threading.Lock`` (sync-in-async) mirrors
``tg_dispatch_counter`` so it survives any future worker-thread caller.

State is intentionally in-memory and volatile: ``_paced_until`` holds
seconds-to-tens-of-seconds backoff deadlines that are far shorter than the process
restart cadence, and the very next 429 re-establishes a deadline. Losing it on
restart self-heals within one request — do NOT add disk persistence (this is
transient backoff state, distinct from the §12a audit-row persistence concern).
"""

from __future__ import annotations

import time
from threading import Lock

# Telegram sometimes omits retry_after on a 429; pace a conservative 1s.
_DEFAULT_RETRY_AFTER = 1.0

_paced_until: dict[str, float] = {}
_lock = Lock()


def register_429(
    chat_id: str, retry_after: float | None, *, now: float | None = None
) -> float:
    """Pace ``chat_id`` for ``retry_after`` seconds. Keeps the LATER of any
    existing deadline and ``now + retry_after``. Returns the paced-until
    monotonic deadline."""
    now = time.monotonic() if now is None else now
    wait = (
        float(retry_after) if retry_after and retry_after > 0 else _DEFAULT_RETRY_AFTER
    )
    with _lock:
        deadline = max(_paced_until.get(chat_id, 0.0), now + wait)
        _paced_until[chat_id] = deadline
        return deadline


def pacing_wait_seconds(chat_id: str, *, now: float | None = None) -> float:
    """Seconds to wait before sending to ``chat_id`` (0.0 if not paced)."""
    now = time.monotonic() if now is None else now
    with _lock:
        until = _paced_until.get(chat_id, 0.0)
    return max(0.0, until - now)


def reset_for_tests() -> None:
    with _lock:
        _paced_until.clear()
