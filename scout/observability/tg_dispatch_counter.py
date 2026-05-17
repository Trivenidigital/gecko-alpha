"""TGDispatchCounter — in-memory rolling-window counter for TG dispatch
observability. Measurement-layer only; does NOT rate-limit or pace.

BL-NEW-TG-BURST-PROFILE (cycle 3). 4-week measurement window; operator
decides PACE-vs-ACCEPT per the pre-registered criteria in
`tasks/plan_tg_burst_profile.md` § Decision criteria. Filed follow-up:
`BL-NEW-TG-PACING-DECISION` (decision-by 2026-06-14).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
# V13 plan-review fold: threading.Lock not asyncio.Lock — hook is
# sync-inside-async (record_dispatch is called BEFORE any await in
# alerter.send_telegram_message). Must survive any future thread-spawn
# caller (e.g., scout/trading/* worker threads). asyncio.Lock would
# break the multi-thread path.
from threading import Lock

import structlog

logger = structlog.get_logger()

# Telegram documented Bot API limits:
# - 1 message per second per chat
# - 20 messages per minute to the same GROUP CHAT (group chat_ids start with '-')
# - DMs (positive chat_ids) tolerate higher rates (~30/sec per FAQ)
# Source: https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this
_ONE_SECOND = 1.0
_ONE_MINUTE = 60.0


def _is_group_chat(chat_id: str) -> bool:
    """Telegram convention: group/channel chat_ids are negative integers.

    V13 plan-review fold: production currently sends to DM `6337722878`
    (positive) per memory `project_telegram_wired_2026_05_06.md`. The
    20/min threshold only applies to groups; DM bursts up to ~30/sec are
    tolerated by Telegram. Apply 20/min check ONLY to group chats.
    """
    return chat_id.lstrip().startswith("-")


class TGDispatchCounter:
    """Per-(chat_id, source) rolling-window counter."""

    def __init__(self) -> None:
        self._per_key: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = Lock()
        self._total_calls = 0

    def record(self, chat_id: str, source: str = "unattributed") -> dict:
        """Record a dispatch + return current rolling-window counts."""
        now = time.monotonic()
        key = (chat_id, source)
        with self._lock:
            window = self._per_key[key]
            window.append(now)
            cutoff_1m = now - _ONE_MINUTE
            while window and window[0] < cutoff_1m:
                window.popleft()
            cutoff_1s = now - _ONE_SECOND
            count_1s = sum(1 for t in window if t >= cutoff_1s)
            count_1m = len(window)
            self._total_calls += 1
            total = self._total_calls

        return {
            "chat_id": chat_id,
            "source": source,
            "count_1s": count_1s,
            "count_1m": count_1m,
            "total_calls": total,
        }


# Module-level singleton.
_counter = TGDispatchCounter()


def record_dispatch(chat_id: str, source: str = "unattributed") -> None:
    """Hook called by scout.alerter.send_telegram_message on every dispatch.

    Emits `tg_dispatch_observed` at DEBUG (V13 fold — default-INFO journalctl
    filters it out; only opt-in operators see per-call rows). Emits
    `tg_burst_observed` at WARNING when:
    - count_1s > 1 (per-chat 1msg/sec — applies to all chats)
    - count_1m > 20 AND chat is a group (V13 fold — DMs tolerate higher rates)
    """
    stats = _counter.record(chat_id, source)
    logger.debug("tg_dispatch_observed", **stats)
    is_group = _is_group_chat(chat_id)
    breached_1s = stats["count_1s"] > 1
    breached_1m = is_group and stats["count_1m"] > 20
    if breached_1s or breached_1m:
        logger.warning(
            "tg_burst_observed",
            **stats,
            breached_1s=breached_1s,
            breached_1m=breached_1m,
            is_group=is_group,
        )


def record_429(
    chat_id: str,
    source: str = "unattributed",
    retry_after: int | None = None,
) -> None:
    """Hook called by alerter on Telegram 429 HTTP response.

    V14 plan-review MUST-FIX: measure punishment, not approach. A 429 from
    Telegram is the only firm pacing trigger — count_1m approaching the
    documented limit doesn't mean Telegram actually penalized us.
    """
    logger.warning(
        "tg_dispatch_rejected_429",
        chat_id=chat_id,
        source=source,
        retry_after=retry_after,
    )


def reset_for_tests() -> None:
    """Test helper: clear counter state between tests."""
    global _counter
    _counter = TGDispatchCounter()
