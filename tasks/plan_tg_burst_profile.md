**New primitives introduced:** `scout/observability/tg_dispatch_counter.py` module with `TGDispatchCounter` class (in-memory deque-based rolling window per chat_id), structured log events `tg_dispatch_observed` (every call) + `tg_burst_observed` (threshold-breach), instrumentation hook in `scout.alerter.send_telegram_message`, optional `TG_BURST_PROFILE_ENABLED` Settings field (default True for the 4-week measurement window), operator helper script `scripts/tg_burst_summary.sh`.

# Plan: BL-NEW-TG-BURST-PROFILE — instrument Telegram dispatch burst frequency

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure per-cycle TG dispatch volume + burst frequency at gecko-alpha's 13+ dispatch sites without changing dispatch behavior. Decision-bearing data: do we need pacing? Backlog filing (BL-NEW-TG-BURST-PROFILE) explicitly says "burst frequency unmeasured."

**Architecture:** Lightweight counter module attached to `scout.alerter.send_telegram_message`. Per-call timestamp recorded in a deque per chat_id. On each call, check rolling-window counts vs Telegram's documented limits (1 msg/sec per chat, 20 msgs/min per group chat). Emit structured logs that survive journalctl rotation by being grep-able. No DB persistence (per CLAUDE.md "Don't add features beyond what the task requires") — measurement is journalctl-based for the 4-week window; if persistence is needed later, file as follow-up.

**Tech Stack:** structlog, pure-Python `collections.deque`, Pydantic Settings.

---

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Telegram dispatch rate observability / burst metrics | None — Hermes Skill Hub (DevOps + Social Media + Productivity categories) returns no skill covering Telegram per-recipient send instrumentation. Verified 2026-05-17. | Build in-tree. |
| Generic rolling-window counters | None — Python stdlib `collections.deque` is sufficient; no Hermes skill is necessary. | Build in-tree. |

awesome-hermes-agent: 404 (consistent with prior probes). **Verdict:** custom-code path warranted; cheap measurement layer.

---

## Drift check (post-fetch)

- **Step 0:** `git fetch && git log --oneline -10 origin/master` → top commits are PR #138 (`c4d0859` cycle 2 prune merge) + `23cd8e0` baseline-fix merge. Nothing newer touching TG dispatch.
- Grep `tg_dispatch|tg_burst|telegram_dispatch_count|tg_send_count` across `scout/` → no matches. Net-new instrumentation surface.
- `scout/alerter.py:137` (`send_telegram_message`) — the single dispatch point used by 13+ call sites per memory `findings_silent_failure_audit_2026_05_11.md` §2.9 + cycle 1's parse-mode audit. Only one hook point needed.

**Backlog reference:** `BL-NEW-TG-BURST-PROFILE` filed 2026-05-13 from BL-NEW-CYCLE-CHANGE-AUDIT (PR #114). decision-by: 4 weeks (so by ~2026-06-10).

---

## Reader-window analysis

| Consumer | What it reads | Window |
|---|---|---|
| Operator (manual) | `journalctl -u gecko-pipeline \| grep tg_dispatch_observed` | journalctl default ~30d retention |
| Operator (manual) | `journalctl ... \| grep tg_burst_observed` | same |
| Follow-up scoring (out of scope) | `tg_dispatch_observed` events for aggregation | TBD post-measurement |

No code consumer exists or is planned in this PR. journalctl is the read surface. The 30-day retention is well within the 4-week measurement window.

---

## File map

- **Create:**
  - `scout/observability/__init__.py` (empty package marker — observability is currently scattered; this groups the counter cleanly)
  - `scout/observability/tg_dispatch_counter.py` — `TGDispatchCounter` class + module-level singleton
  - `scripts/tg_burst_summary.sh` — operator helper: greps journalctl for `tg_dispatch_observed` + `tg_burst_observed` events over a configurable window, prints summary stats
  - `tests/test_tg_dispatch_counter.py` — unit tests for the counter logic
- **Modify:**
  - `scout/alerter.py` — hook the counter in `send_telegram_message`; toggle via `settings.TG_BURST_PROFILE_ENABLED`
  - `scout/config.py` — add `TG_BURST_PROFILE_ENABLED: bool = True`
  - `tests/test_config.py` — default test for the new flag

---

## Tasks

### Task 1: Settings flag

**Files:** `scout/config.py`, `tests/test_config.py`

- [ ] **Step 1.1: Failing test**

```python
def test_tg_burst_profile_enabled_default_true():
    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
    )
    assert s.TG_BURST_PROFILE_ENABLED is True


def test_tg_burst_profile_enabled_env_override():
    s = Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="c",
        ANTHROPIC_API_KEY="k",
        TG_BURST_PROFILE_ENABLED=False,
    )
    assert s.TG_BURST_PROFILE_ENABLED is False
```

- [ ] **Step 1.2: Run → FAIL**

- [ ] **Step 1.3: Add field in `scout/config.py`** (near other observability flags):

```python
    # BL-NEW-TG-BURST-PROFILE: per-call instrumentation for TG dispatch
    # frequency. Default True for the 4-week measurement window; toggle
    # False via .env to disable if instrumentation overhead surfaces.
    TG_BURST_PROFILE_ENABLED: bool = True
```

- [ ] **Step 1.4: Run → PASS. Commit.**

---

### Task 2: `TGDispatchCounter` module

**Files:** `scout/observability/__init__.py` (new), `scout/observability/tg_dispatch_counter.py` (new), `tests/test_tg_dispatch_counter.py` (new)

**Design:**

```python
# scout/observability/tg_dispatch_counter.py
from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

import structlog

logger = structlog.get_logger()

# Telegram documented limits:
# - 1 message per second per chat
# - 20 messages per minute to the same group chat
# Source: https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this
_ONE_SECOND = 1.0
_ONE_MINUTE = 60.0


class TGDispatchCounter:
    """In-memory rolling-window counter for Telegram dispatch observability.

    Records every send_telegram_message call. Emits two structured log events:
    - `tg_dispatch_observed` per call with current 1s + 60s same-chat counts
    - `tg_burst_observed` when either threshold is breached on the calling row

    NOT a rate-limiter — it does NOT block or delay calls. Measurement only.
    BL-NEW-TG-BURST-PROFILE collects 4 weeks of data, then operator decides
    whether to add pacing.

    Counter state is in-memory and resets on process restart (per CLAUDE.md
    feedback_in_memory_telemetry_persistence.md — acceptable for measurement
    since journalctl retains the structured logs across restarts).
    """

    def __init__(self) -> None:
        self._per_chat: dict[str, deque[float]] = defaultdict(deque)
        self._lock = Lock()
        self._total_calls = 0

    def record(self, chat_id: str) -> dict:
        """Record a dispatch + return current rolling-window counts.

        Returns dict with: chat_id, count_1s, count_1m, total_calls
        (cumulative since process start).
        """
        now = time.monotonic()
        with self._lock:
            window = self._per_chat[chat_id]
            window.append(now)
            # Evict timestamps older than 60s
            cutoff_1m = now - _ONE_MINUTE
            while window and window[0] < cutoff_1m:
                window.popleft()
            # Count subset within last 1s
            cutoff_1s = now - _ONE_SECOND
            count_1s = sum(1 for t in window if t >= cutoff_1s)
            count_1m = len(window)
            self._total_calls += 1
            total = self._total_calls

        return {
            "chat_id": chat_id,
            "count_1s": count_1s,
            "count_1m": count_1m,
            "total_calls": total,
        }


# Module-level singleton. Created on first import. Reset implicitly on
# process restart. NOT persisted (measurement is journalctl-based).
_counter = TGDispatchCounter()


def record_dispatch(chat_id: str) -> None:
    """Hook called by scout.alerter.send_telegram_message on every dispatch.

    Emits `tg_dispatch_observed` always, and `tg_burst_observed` when either
    Telegram-documented threshold is breached:
    - count_1s > 1 (per-chat 1msg/sec limit)
    - count_1m > 20 (per-group 20msg/min limit)
    """
    stats = _counter.record(chat_id)
    logger.info("tg_dispatch_observed", **stats)
    if stats["count_1s"] > 1 or stats["count_1m"] > 20:
        logger.warning(
            "tg_burst_observed",
            **stats,
            breached_1s=stats["count_1s"] > 1,
            breached_1m=stats["count_1m"] > 20,
        )


def reset_for_tests() -> None:
    """Test helper: clear counter state between tests."""
    global _counter
    _counter = TGDispatchCounter()
```

- [ ] **Step 2.1: Failing tests**

```python
# tests/test_tg_dispatch_counter.py
import structlog


def test_record_single_dispatch_emits_observed_log():
    from scout.observability.tg_dispatch_counter import record_dispatch, reset_for_tests

    reset_for_tests()
    with structlog.testing.capture_logs() as logs:
        record_dispatch("chat-1")

    events = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert len(events) == 1
    assert events[0]["chat_id"] == "chat-1"
    assert events[0]["count_1s"] == 1
    assert events[0]["count_1m"] == 1
    assert events[0]["total_calls"] == 1


def test_record_two_in_one_second_triggers_burst_observed():
    from scout.observability.tg_dispatch_counter import record_dispatch, reset_for_tests

    reset_for_tests()
    with structlog.testing.capture_logs() as logs:
        record_dispatch("chat-1")
        record_dispatch("chat-1")

    burst_events = [e for e in logs if e.get("event") == "tg_burst_observed"]
    assert len(burst_events) == 1, f"expected 1 burst, got {len(burst_events)}: {logs}"
    assert burst_events[0]["count_1s"] == 2
    assert burst_events[0]["breached_1s"] is True
    assert burst_events[0]["breached_1m"] is False


def test_per_chat_isolation():
    """Two different chats — neither should trigger a burst on the other."""
    from scout.observability.tg_dispatch_counter import record_dispatch, reset_for_tests

    reset_for_tests()
    with structlog.testing.capture_logs() as logs:
        record_dispatch("chat-A")
        record_dispatch("chat-B")

    burst_events = [e for e in logs if e.get("event") == "tg_burst_observed"]
    assert burst_events == []
    observed = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert len(observed) == 2
    # Each chat sees count_1s = 1 independently
    by_chat = {e["chat_id"]: e for e in observed}
    assert by_chat["chat-A"]["count_1s"] == 1
    assert by_chat["chat-B"]["count_1s"] == 1


def test_eviction_after_60s(monkeypatch):
    """After 61 seconds of fake-monotonic, prior entries should evict."""
    import scout.observability.tg_dispatch_counter as mod
    mod.reset_for_tests()

    # Fake time.monotonic so we can advance the clock without sleeping
    fake_now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: fake_now[0])

    mod.record_dispatch("chat-1")
    fake_now[0] = 1062.0  # 62 seconds later

    with structlog.testing.capture_logs() as logs:
        mod.record_dispatch("chat-1")

    events = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert events[-1]["count_1m"] == 1, "old entry should have been evicted"


def test_thread_safety_under_concurrent_record():
    """Hammer the counter from N threads; assert no exceptions + monotonic total."""
    import threading
    from scout.observability.tg_dispatch_counter import record_dispatch, reset_for_tests, _counter

    reset_for_tests()
    n_threads = 10
    n_per_thread = 100

    def hammer():
        for _ in range(n_per_thread):
            record_dispatch("chat-stress")

    threads = [threading.Thread(target=hammer) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # We can't import _counter directly (module-level singleton); peek via record
    final = record_dispatch.__module__  # type: ignore[attr-defined]
    # Sanity: after N=10*100 calls + 1 final record, total should be > 1000
    # We capture the next call's total_calls
    with structlog.testing.capture_logs() as logs:
        record_dispatch("chat-stress-final")
    events = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert events[-1]["total_calls"] >= n_threads * n_per_thread + 1
```

- [ ] **Step 2.2: Run → FAIL**

- [ ] **Step 2.3: Implement `scout/observability/tg_dispatch_counter.py`** per the design block above.

- [ ] **Step 2.4: Add `scout/observability/__init__.py`** (empty file):

```python
"""Observability primitives: counters, instrumentation hooks. Decoupled
from scout.alerter and scout.main to keep imports light."""
```

- [ ] **Step 2.5: Run → PASS. Commit.**

---

### Task 3: Hook into `send_telegram_message`

**Files:** `scout/alerter.py`

- [ ] **Step 3.1: Failing integration test in `tests/test_alerter.py`** (or new file `tests/test_alerter_tg_burst_hook.py`):

```python
import pytest
import structlog
from unittest.mock import AsyncMock, MagicMock

from scout.alerter import send_telegram_message
from scout.observability.tg_dispatch_counter import reset_for_tests


@pytest.mark.asyncio
async def test_send_telegram_message_records_dispatch_when_enabled():
    """When TG_BURST_PROFILE_ENABLED=True, every send emits tg_dispatch_observed."""
    reset_for_tests()
    session = MagicMock()
    session.post.return_value.__aenter__.return_value = MagicMock(status=200)
    settings = MagicMock(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="chat-X",
        TG_BURST_PROFILE_ENABLED=True,
    )

    with structlog.testing.capture_logs() as logs:
        await send_telegram_message("hello", session, settings, parse_mode=None)

    observed = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert len(observed) == 1
    assert observed[0]["chat_id"] == "chat-X"


@pytest.mark.asyncio
async def test_send_telegram_message_skips_counter_when_disabled():
    reset_for_tests()
    session = MagicMock()
    session.post.return_value.__aenter__.return_value = MagicMock(status=200)
    settings = MagicMock(
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="chat-X",
        TG_BURST_PROFILE_ENABLED=False,
    )

    with structlog.testing.capture_logs() as logs:
        await send_telegram_message("hello", session, settings, parse_mode=None)

    observed = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert observed == []
```

- [ ] **Step 3.2: Run → FAIL**

- [ ] **Step 3.3: Modify `scout/alerter.py:send_telegram_message`** to call `record_dispatch(settings.TELEGRAM_CHAT_ID)` BEFORE the HTTP call (so we instrument the intent-to-dispatch even if the HTTP call fails — burst pressure is about call rate, not delivery success).

```python
# At top of scout/alerter.py:
from scout.observability.tg_dispatch_counter import record_dispatch

# Inside send_telegram_message, BEFORE the HTTP call (~line 154 after _truncate):
if settings.TG_BURST_PROFILE_ENABLED:
    record_dispatch(str(settings.TELEGRAM_CHAT_ID))
```

- [ ] **Step 3.4: Run → PASS. Commit.**

---

### Task 4: Operator helper script

**Files:** `scripts/tg_burst_summary.sh` (new)

- [ ] **Step 4.1: Add operator-friendly summary script**

```bash
#!/usr/bin/env bash
# tg_burst_summary.sh — summarize TG dispatch instrumentation from journalctl.
# Usage: ./scripts/tg_burst_summary.sh [hours-back]   (default: 24)
# Requires: jq.
#
# Filed BL-NEW-TG-BURST-PROFILE (decision-by 2026-06-10).
set -euo pipefail
HOURS="${1:-24}"
SINCE="${HOURS} hours ago"

echo "=== TG dispatch summary, last ${HOURS}h ==="
echo

OBSERVED_COUNT=$(journalctl -u gecko-pipeline --since "$SINCE" 2>/dev/null \
    | grep -c '"event": "tg_dispatch_observed"' || true)
BURST_COUNT=$(journalctl -u gecko-pipeline --since "$SINCE" 2>/dev/null \
    | grep -c '"event": "tg_burst_observed"' || true)

echo "Total dispatches: $OBSERVED_COUNT"
echo "Burst events (1s OR 1m threshold breached): $BURST_COUNT"
echo

if [[ "$BURST_COUNT" -gt 0 ]]; then
    echo "--- Burst breakdown by chat_id ---"
    journalctl -u gecko-pipeline --since "$SINCE" 2>/dev/null \
        | grep '"event": "tg_burst_observed"' \
        | jq -r '"\(.chat_id) breached_1s=\(.breached_1s) breached_1m=\(.breached_1m) count_1s=\(.count_1s) count_1m=\(.count_1m)"' \
        | sort | uniq -c | sort -rn | head -20
    echo
fi

echo "--- Top 10 chat_ids by dispatch count ---"
journalctl -u gecko-pipeline --since "$SINCE" 2>/dev/null \
    | grep '"event": "tg_dispatch_observed"' \
    | jq -r '.chat_id' \
    | sort | uniq -c | sort -rn | head -10
```

- [ ] **Step 4.2: Commit**

---

### Task 5: Backlog status close

**Files:** `backlog.md`

- [ ] Update `BL-NEW-TG-BURST-PROFILE` status to SHIPPED with branch ref + decision-by note: "structured logs landed; 4-week soak ends ~2026-06-15; operator runs `scripts/tg_burst_summary.sh` periodically; decision (add pacing or accept current behavior) recorded by 2026-06-15."

---

## Test plan summary

- 2 config tests (default + env override)
- 5 counter-module tests (single dispatch, two-in-1s burst, per-chat isolation, eviction after 60s, thread-safety under concurrent record)
- 2 alerter integration tests (records when enabled, skips when disabled)
- Full regression must pass

Total: 9 new tests.

---

## Deployment verification (autonomous post-3-reviewer-fold)

1. `journalctl -u gecko-pipeline --since "5 minutes ago" | grep '"event": "tg_dispatch_observed"' | head -3` — confirm structured logs after restart (any TG dispatch within 5min should show)
2. `./scripts/tg_burst_summary.sh 1` — operator-friendly summary; expect ≥0 dispatches in last 1h depending on activity
3. Revert path: `TG_BURST_PROFILE_ENABLED=False` in `.env` + restart — disables the counter call. Full revert = revert PR.

---

## Out of scope

- DB persistence of dispatch counters — `feedback_in_memory_telemetry_persistence.md` warns that module-level counters reset on restart. Acceptable for measurement: journalctl retains structured events across restarts; aggregate via `scripts/tg_burst_summary.sh`. Persistence becomes scope only IF operator chooses to act on the data (file as follow-up at that decision).
- Active pacing / rate-limiting — explicitly NOT this PR's scope. Pacing decision is for BL-NEW-PRUNE-PACING-FOLLOWUP-equivalent follow-up after 4-week data.
- §12a watchdog on `tg_dispatch_observed` — measurement table doesn't exist; covered by the deferred §12a daemon item.
- Per-chat_id rate-limiting at the alerter layer — measurement first; intervention is a separate decision.
