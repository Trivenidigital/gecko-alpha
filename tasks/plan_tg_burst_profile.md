**New primitives introduced:** `scout/observability/tg_dispatch_counter.py` module with `TGDispatchCounter` class (in-memory deque-based rolling window per chat_id), structured log events `tg_dispatch_observed` (debug, every call) + `tg_burst_observed` (warning, threshold-breach) + `tg_dispatch_rejected_429` (warning, Telegram 429 response), instrumentation hook + `source:` kwarg in `scout.alerter.send_telegram_message` (caller passes its dispatch-site label for attribution), optional `TG_BURST_PROFILE_ENABLED` Settings field (default True for the 4-week measurement window), operator helper script `scripts/tg_burst_summary.sh` (now with time-of-day histogram + top-K callsites + 429-correlation), `scripts/tg_burst_archive.sh` cron'd weekly to dump events to `/var/log/gecko-alpha/tg-burst-archive/` (V14 fold for journalctl-retention insurance), pre-registered decision criteria (PACE if any Telegram 429 observed OR `tg_burst_observed` >50/week per group-chat callsite over 4 weeks; ACCEPT otherwise). Filed follow-up: `BL-NEW-TG-PACING-DECISION` with these criteria.

## Decision criteria (pre-registered per V14 fold)

After the 4-week measurement window (~2026-06-14):

| Condition | Action |
|---|---|
| Any `tg_dispatch_rejected_429` event observed | **PACE** (Telegram actually rejected — limit was hit) |
| `tg_burst_observed` (group-chat callsite) > 50/week sustained | **PACE** (high near-miss rate suggests we're close to the limit) |
| `tg_burst_observed` fires but only on DM (`TELEGRAM_CHAT_ID=6337722878` per memory `project_telegram_wired_2026_05_06.md`) AND zero 429s | **ACCEPT** — 1-on-1 DMs have higher Telegram limits (~30/sec per FAQ); breach on `count_1m > 20` is conservative, not actionable |
| Zero `tg_burst_observed` OR `tg_dispatch_rejected_429` in 4 weeks | **ACCEPT** (no burst pressure observed) |

Edge: if `tg_burst_observed` fires only sporadically (<10/week) AND no 429s, ACCEPT but extend monitoring 4 more weeks via the archive script. This pre-registration anchors investigation per memory `feedback_pre_registered_hypothesis_anchoring.md`.

# Plan: BL-NEW-TG-BURST-PROFILE — instrument Telegram dispatch burst frequency

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure per-cycle TG dispatch volume + burst frequency at gecko-alpha's 13+ dispatch sites without changing dispatch behavior. Decision-bearing data: do we need pacing? Backlog filing (BL-NEW-TG-BURST-PROFILE) explicitly says "burst frequency unmeasured."

**Architecture:** Lightweight counter module attached to `scout.alerter.send_telegram_message`. Per-call timestamp recorded in a deque per (chat_id, source) tuple. On each call, check rolling-window counts vs Telegram's documented limits (1 msg/sec per chat, 20 msgs/min per group chat). Emit structured logs at `debug` for routine observations (filtered out at default INFO level — V13 fold) and `warning` for threshold breaches OR 429 responses (always-visible signal). `source:` kwarg propagates the dispatch-site label so the operator can attribute bursts to the noisy caller (V14 SHOULD-FIX fold). journalctl is the primary read surface for the 4-week window; weekly `tg_burst_archive.sh` cron dumps to `/var/log/gecko-alpha/tg-burst-archive/` as insurance against journalctl rotation under burst load.

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
  - `scout/observability/__init__.py` (empty package marker)
  - `scout/observability/tg_dispatch_counter.py` — `TGDispatchCounter` class + module-level singleton
  - `scripts/tg_burst_summary.sh` — operator helper with time-of-day histogram + top-K callsites + 429 correlation (V14 fold)
  - `scripts/tg_burst_archive.sh` — weekly cron: dump `tg_dispatch_observed`/`tg_burst_observed`/`tg_dispatch_rejected_429` events to `/var/log/gecko-alpha/tg-burst-archive/$(date +%Y-%m-%d).jsonl.gz` (V14 fold — journalctl rotation insurance)
  - `tests/test_tg_dispatch_counter.py` — unit tests
- **Modify:**
  - `scout/alerter.py` — hook + `source:` kwarg propagation (default `"unattributed"`)
  - `scout/config.py` — add `TG_BURST_PROFILE_ENABLED: bool = True`
  - `tests/test_config.py` — default test
- **No code changes (filing only):**
  - `backlog.md` — file `BL-NEW-TG-PACING-DECISION` with pre-registered criteria (V14 fold). The current PR ships the measurement; the decision happens after 4-week soak.

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
# V13 fold: threading.Lock not asyncio.Lock — hook is sync-inside-async
# (record_dispatch is called BEFORE any await in alerter.send_telegram_message).
# Must survive any future thread-spawn caller (e.g. scout/trading/* worker
# threads). asyncio.Lock would break the multi-thread path.
from threading import Lock

import structlog

logger = structlog.get_logger()

# Telegram documented limits (Bot API):
# - 1 message per second per chat
# - 20 messages per minute to the same GROUP CHAT (group chat_ids start with '-')
# - DMs (positive chat_ids) have higher limits (~30/sec per FAQ)
# Source: https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this
_ONE_SECOND = 1.0
_ONE_MINUTE = 60.0


def _is_group_chat(chat_id: str) -> bool:
    """Telegram convention: group/channel chat_ids are negative integers.

    V13 fold: production currently sends to DM `6337722878` (positive) per
    memory `project_telegram_wired_2026_05_06.md`. The 20/min threshold
    only applies to groups; DM bursts up to ~30/sec are tolerated by Telegram.
    Apply the 20/min check ONLY to group chats.
    """
    return chat_id.lstrip().startswith("-")


class TGDispatchCounter:
    """In-memory rolling-window counter for Telegram dispatch observability.

    Records every send_telegram_message call. Emits structured log events:
    - `tg_dispatch_observed` (debug) per call with current 1s + 60s counts
    - `tg_burst_observed` (warning) when threshold breached
    - `tg_dispatch_rejected_429` (warning) on Telegram 429 response (called
      separately by alerter on the response path)

    NOT a rate-limiter — measurement only. BL-NEW-TG-BURST-PROFILE collects
    4 weeks; operator decision per pre-registered criteria in
    `tasks/plan_tg_burst_profile.md`.
    """

    def __init__(self) -> None:
        # Keyed on (chat_id, source) — V14 fold: callsite attribution.
        self._per_key: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        self._lock = Lock()
        self._total_calls = 0

    def record(self, chat_id: str, source: str = "unattributed") -> dict:
        """Record a dispatch + return current rolling-window counts.

        Returns dict with: chat_id, source, count_1s, count_1m, total_calls.
        """
        now = time.monotonic()
        key = (chat_id, source)
        with self._lock:
            window = self._per_key[key]
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


def record_429(chat_id: str, source: str = "unattributed",
               retry_after: int | None = None) -> None:
    """Hook called by alerter on Telegram 429 HTTP response.

    V14 fold MUST-FIX #2: measure punishment, not approach. A 429 from
    Telegram is the only firm pacing trigger — count_1m approaching the
    limit doesn't mean Telegram actually penalized us.
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
    """V13 fold MUST-FIX #2: hammer the counter from N threads; assert
    EXACT equality on the next call's total_calls — proves no calls were
    lost to a race. Previous version had dead code (`record_dispatch.__module__`)
    and a >= assertion that wouldn't catch lost-update bugs.

    V13 fold SHOULD-FIX: do NOT `from ... import _counter` — module-level
    singleton import binds the OLD object into test-local namespace and
    becomes a footgun for future tests. Only import the public surface.
    """
    import threading
    from scout.observability.tg_dispatch_counter import record_dispatch, reset_for_tests

    reset_for_tests()
    n_threads = 10
    n_per_thread = 100

    def hammer():
        for _ in range(n_per_thread):
            record_dispatch("chat-stress", source="stress-test")

    threads = [threading.Thread(target=hammer) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # The (N=10*100=1000) hammered calls populated _total_calls. The 1001st
    # call here must observe exactly that count via stats["total_calls"].
    with structlog.testing.capture_logs() as logs:
        record_dispatch("chat-stress-final", source="stress-test")
    events = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert events[-1]["total_calls"] == n_threads * n_per_thread + 1, (
        f"Race lost updates: expected {n_threads * n_per_thread + 1}, "
        f"got {events[-1]['total_calls']}"
    )


def test_dm_does_not_trigger_1m_burst(monkeypatch):
    """V13 fold: DM chats (positive chat_id) tolerate higher rates than the
    20/min group-chat limit. 21+ dispatches to a DM must NOT emit
    `tg_burst_observed` for breached_1m."""
    import scout.observability.tg_dispatch_counter as mod

    mod.reset_for_tests()
    # Fake monotonic so all 21 calls land in the same 1-second eviction window
    # at progressing-by-1ms timestamps (no 1-sec breach interference).
    t0 = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: t0[0])

    with structlog.testing.capture_logs() as logs:
        for i in range(21):
            t0[0] = 1000.0 + i * 1.2  # space 1.2s apart — avoids count_1s>1
            mod.record_dispatch("6337722878", source="test")  # DM (positive)

    burst_events = [e for e in logs if e.get("event") == "tg_burst_observed"]
    # No 1s breach (calls 1.2s apart), and DM should not 1m-breach
    assert burst_events == [], f"DM should not trigger 1m burst, got: {burst_events}"


def test_group_chat_triggers_1m_burst_above_20(monkeypatch):
    """V13 fold: group chats (negative chat_id) DO trigger 1m burst above 20."""
    import scout.observability.tg_dispatch_counter as mod

    mod.reset_for_tests()
    t0 = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: t0[0])

    with structlog.testing.capture_logs() as logs:
        for i in range(21):
            t0[0] = 1000.0 + i * 1.2
            mod.record_dispatch("-1001234567890", source="test")  # group

    burst_events = [e for e in logs if e.get("event") == "tg_burst_observed"]
    assert len(burst_events) >= 1
    assert burst_events[-1]["breached_1m"] is True
    assert burst_events[-1]["is_group"] is True


def test_record_429_emits_rejected_event():
    """V14 fold MUST-FIX #2: 429-from-Telegram is the firm pacing trigger."""
    from scout.observability.tg_dispatch_counter import record_429, reset_for_tests

    reset_for_tests()
    with structlog.testing.capture_logs() as logs:
        record_429("6337722878", source="daily-summary", retry_after=15)

    events = [e for e in logs if e.get("event") == "tg_dispatch_rejected_429"]
    assert len(events) == 1
    assert events[0]["retry_after"] == 15
    assert events[0]["source"] == "daily-summary"
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

- [ ] **Step 3.1: Failing integration test in `tests/test_alerter.py`** (extend existing file — uses the `aioresponses` idiom already established at `tests/test_alerter.py:5,12,100,194`):

```python
# Append to tests/test_alerter.py — uses aioresponses + real aiohttp.ClientSession.
# V15 M1 fold: do NOT use MagicMock for session — aiohttp's async context manager
# semantics require AsyncMock chains that are easy to get wrong; aioresponses
# is the project idiom and matches existing test_alerter.py tests.
import aiohttp
import pytest
import structlog
from aioresponses import aioresponses

from scout.alerter import send_telegram_message
from scout.config import Settings
from scout.observability.tg_dispatch_counter import reset_for_tests


def _settings(enabled: bool = True) -> Settings:
    """Spec-aware Settings (catches typos in new field name via attribute check)."""
    return Settings(
        _env_file=None,
        TELEGRAM_BOT_TOKEN="t",
        TELEGRAM_CHAT_ID="6337722878",   # DM — matches prod
        ANTHROPIC_API_KEY="k",
        TG_BURST_PROFILE_ENABLED=enabled,
    )


@pytest.mark.asyncio
async def test_send_telegram_message_records_dispatch_when_enabled():
    """V15 M1 fold: aioresponses + real ClientSession matches project test idiom."""
    reset_for_tests()
    settings = _settings(enabled=True)
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

    with aioresponses() as m:
        m.post(url, status=200, payload={"ok": True})
        async with aiohttp.ClientSession() as session:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message(
                    "hello", session, settings,
                    parse_mode=None, source="test-suite",
                )

    observed = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert len(observed) == 1
    assert observed[0]["chat_id"] == "6337722878"
    assert observed[0]["source"] == "test-suite"


@pytest.mark.asyncio
async def test_send_telegram_message_skips_counter_when_disabled():
    reset_for_tests()
    settings = _settings(enabled=False)
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

    with aioresponses() as m:
        m.post(url, status=200, payload={"ok": True})
        async with aiohttp.ClientSession() as session:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message(
                    "hello", session, settings, parse_mode=None
                )

    observed = [e for e in logs if e.get("event") == "tg_dispatch_observed"]
    assert observed == []


@pytest.mark.asyncio
async def test_send_telegram_message_records_429_with_retry_after():
    """V14 MUST-FIX + V15 M3 fold: 429 response captures retry_after BEFORE
    body is consumed by error-path logging."""
    reset_for_tests()
    settings = _settings(enabled=True)
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

    with aioresponses() as m:
        m.post(url, status=429, payload={
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 15},
        })
        async with aiohttp.ClientSession() as session:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message(
                    "hello", session, settings,
                    parse_mode=None, source="429-test",
                )

    rejected = [e for e in logs if e.get("event") == "tg_dispatch_rejected_429"]
    assert len(rejected) == 1
    assert rejected[0]["retry_after"] == 15
    assert rejected[0]["source"] == "429-test"


@pytest.mark.asyncio
async def test_send_telegram_message_instrumentation_failure_is_isolated(monkeypatch):
    """V15 M2 fold: if record_429 raises (instrumentation regression),
    the alerter must NOT swallow it under the outer try/except — must emit
    a distinct logger.exception so operators can spot instrumentation drift."""
    reset_for_tests()
    settings = _settings(enabled=True)
    url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"

    import scout.alerter as alerter_mod
    monkeypatch.setattr(
        alerter_mod, "record_429",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("instr broken")),
    )

    with aioresponses() as m:
        m.post(url, status=429, payload={
            "ok": False, "parameters": {"retry_after": 5},
        })
        async with aiohttp.ClientSession() as session:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message(
                    "hello", session, settings, parse_mode=None,
                )

    # Should emit the dedicated record_429_failed exception event
    iso_failed = [e for e in logs if e.get("event") == "record_429_failed"]
    assert len(iso_failed) == 1
```

- [ ] **Step 3.2: Run → FAIL**

- [ ] **Step 3.3: Modify `scout/alerter.py:send_telegram_message`** — add `source:` kwarg + record both intent-to-dispatch AND 429 response. V15 M3 fold: read body bytes ONCE before parsing for either path (avoids `resp.json()`+`resp.text()` double-consume). V15 M2 fold: wrap `record_429` call in its own try/except so instrumentation failure can't poison the alerter's existing 429 handling.

```python
# At top of scout/alerter.py:
import json   # for body parsing in 429 path
from scout.observability.tg_dispatch_counter import record_dispatch, record_429

# Modify signature (add source kwarg-only):
async def send_telegram_message(
    text: str,
    session: aiohttp.ClientSession,
    settings: Settings,
    *,
    parse_mode: str | None = "Markdown",
    raise_on_failure: bool = False,
    source: str = "unattributed",   # V14 fold: callsite attribution
) -> None:

# Inside, BEFORE the HTTP call (~line 154 after _truncate):
if settings.TG_BURST_PROFILE_ENABLED:
    record_dispatch(str(settings.TELEGRAM_CHAT_ID), source=source)

# Modify the response-handling block — read body bytes once:
async with session.post(url, json=payload) as resp:
    body_bytes = await resp.read() if resp.status != 200 else None
    if resp.status == 429 and body_bytes is not None:
        retry_after = None
        try:
            body_json = json.loads(body_bytes)
            retry_after = body_json.get("parameters", {}).get("retry_after")
        except (json.JSONDecodeError, ValueError):
            pass
        if settings.TG_BURST_PROFILE_ENABLED:
            try:
                record_429(
                    str(settings.TELEGRAM_CHAT_ID),
                    source=source,
                    retry_after=retry_after,
                )
            except Exception:
                logger.exception("record_429_failed")
    if resp.status != 200:
        body = (
            body_bytes.decode("utf-8", errors="replace")[:200] if body_bytes else ""
        )
        logger.warning(
            "Telegram daily summary failed", status=resp.status, body=body
        )
        # ...rest of existing non-200 handling preserved
```

Callers that pass a `source=` label get attribution; legacy callers default to `"unattributed"`. Hot callsites to label in a separate doc-only follow-up commit (see V15 S1 caller audit in design D1).

Callers that pass a `source=` label get attribution; legacy callers default to `"unattributed"`. Hot callers to label in a separate doc-only follow-up commit: `daily-summary` (main.py:1521), `auto-suspend` (trading/auto_suspend.py:272,327), `calibrate-dryrun` (trading/calibrate.py:354), `bl-064-social` (social/telegram/listener.py:*), `narrative-learn` (narrative/agent.py:557,715), `secondwave` (secondwave/detector.py:285), `weekly-digest` (trading/weekly_digest.py:335,340), `velocity` (velocity/detector.py:193). Operator can grep journalctl by `source` once the labels are passed.

- [ ] **Step 3.4: Run → PASS. Commit.**

---

### Task 4: Operator helper script

**Files:** `scripts/tg_burst_summary.sh` (new)

- [ ] **Step 4.1: Add operator-friendly summary script**

```bash
#!/usr/bin/env bash
# tg_burst_summary.sh — summarize TG dispatch from journalctl + archive.
# V14 fold: time-of-day histogram + top-K callsites + 429 correlation.
# Note: `tg_dispatch_observed` is DEBUG-level (V13 fold) — requires
# either journald set to debug retention OR the archive script's output.
# Usage: ./scripts/tg_burst_summary.sh [hours-back]   (default: 168 = 1 week)
# Requires: jq.
set -euo pipefail
HOURS="${1:-168}"
SINCE="${HOURS} hours ago"
ARCHIVE_DIR="/var/log/gecko-alpha/tg-burst-archive"

echo "=== TG dispatch summary, last ${HOURS}h ==="
echo

# Build the combined event stream from journalctl + any archive files
# that fall within the window. Archive files are jsonl.gz, one event per line.
JOURNAL_EVENTS=$(journalctl -u gecko-pipeline --since "$SINCE" 2>/dev/null \
    | grep -E '"event": "(tg_dispatch_observed|tg_burst_observed|tg_dispatch_rejected_429)"' \
    || true)
ARCHIVE_EVENTS=""
if [[ -d "$ARCHIVE_DIR" ]]; then
    # Pull last N week-files; jq filters by event types.
    ARCHIVE_EVENTS=$(find "$ARCHIVE_DIR" -name '*.jsonl.gz' -mtime "-$((HOURS / 24 + 1))" 2>/dev/null \
        | xargs -r zcat 2>/dev/null \
        | jq -c 'select(.event | test("tg_dispatch_observed|tg_burst_observed|tg_dispatch_rejected_429"))' \
        || true)
fi
COMBINED=$(printf "%s\n%s" "$JOURNAL_EVENTS" "$ARCHIVE_EVENTS" | grep -v '^$' || true)

if [[ -z "$COMBINED" ]]; then
    echo "(no events in window)"
    exit 0
fi

OBSERVED=$(printf "%s\n" "$COMBINED" | grep -c '"event": "tg_dispatch_observed"' || true)
BURST=$(printf "%s\n" "$COMBINED" | grep -c '"event": "tg_burst_observed"' || true)
REJECTED=$(printf "%s\n" "$COMBINED" | grep -c '"event": "tg_dispatch_rejected_429"' || true)

echo "Dispatches: $OBSERVED"
echo "Bursts (threshold breach): $BURST"
echo "429s from Telegram (firm pacing trigger): $REJECTED"
echo

# V14 fold: time-of-day histogram (which hours cluster bursts?)
if [[ "$BURST" -gt 0 || "$REJECTED" -gt 0 ]]; then
    echo "--- Burst+429 events by hour-of-day ---"
    printf "%s\n" "$COMBINED" \
        | jq -r 'select(.event != "tg_dispatch_observed") | .timestamp' \
        | awk -F'T' '{print substr($2,1,2)}' \
        | sort | uniq -c | sort -k2n \
        | awk '{ printf "%s:00  %s\n", $2, $1 }'
    echo
fi

if [[ "$REJECTED" -gt 0 ]]; then
    echo "--- 429 events (firm pacing trigger) ---"
    printf "%s\n" "$COMBINED" \
        | grep '"event": "tg_dispatch_rejected_429"' \
        | jq -r '"\(.timestamp) chat=\(.chat_id) source=\(.source) retry_after=\(.retry_after // "null")"' \
        | head -20
    echo
fi

if [[ "$BURST" -gt 0 ]]; then
    echo "--- Top callsites (source) by burst contribution ---"
    printf "%s\n" "$COMBINED" \
        | grep '"event": "tg_burst_observed"' \
        | jq -r '.source // "unattributed"' \
        | sort | uniq -c | sort -rn | head -10
    echo
fi

echo "--- Top callsites by total dispatch count ---"
printf "%s\n" "$COMBINED" \
    | grep '"event": "tg_dispatch_observed"' \
    | jq -r '.source // "unattributed"' \
    | sort | uniq -c | sort -rn | head -10
```

### Task 4b: Archive script (V14 fold — journalctl-rotation insurance)

**Files:** `scripts/tg_burst_archive.sh` (new)

```bash
#!/usr/bin/env bash
# tg_burst_archive.sh — dump TG burst events to disk weekly.
# Insurance against journalctl rotation under burst load (V14 fold).
# Install: weekly cron under ROOT crontab on srilu — service unit is
# root-owned, journalctl -u gecko-pipeline requires root or systemd-journal
# group membership.
#   30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh
set -euo pipefail
ARCHIVE_DIR="/var/log/gecko-alpha/tg-burst-archive"
mkdir -p "$ARCHIVE_DIR"
chmod 0755 "$ARCHIVE_DIR"
OUT="$ARCHIVE_DIR/$(date +%Y-%m-%d).jsonl.gz"
# V16 SHOULD-FIX #3 fold: 2-week window with overlap so a missed weekly run
# self-recovers next week. Storage cost ~1 MB extra per week (negligible).
journalctl -u gecko-pipeline --since "2 weeks ago" 2>/dev/null \
    | grep -E '"event": "(tg_dispatch_observed|tg_burst_observed|tg_dispatch_rejected_429)"' \
    | gzip > "$OUT"
chmod 0644 "$OUT"
# V16 NICE-TO-HAVE #5 fold: rotate by filename-date, not mtime (rsync/backup
# tools can touch mtimes; filename is the authoritative cohort).
cutoff_epoch=$(date -d "56 days ago" +%s)
for f in "$ARCHIVE_DIR"/*.jsonl.gz; do
    [[ -f "$f" ]] || continue
    base=$(basename "$f" .jsonl.gz)
    # base is YYYY-MM-DD; date -d handles ISO dates portably
    file_epoch=$(date -d "$base" +%s 2>/dev/null || echo 0)
    if [[ "$file_epoch" -gt 0 && "$file_epoch" -lt "$cutoff_epoch" ]]; then
        rm -f "$f"
    fi
done
```

- [ ] **Step 4.2: Commit**

---

### Task 5: Backlog status close + file BL-NEW-TG-PACING-DECISION (V14 fold)

**Files:** `backlog.md`

- [ ] Update `BL-NEW-TG-BURST-PROFILE` status to SHIPPED with branch ref + decision-by note
- [ ] **File new entry `BL-NEW-TG-PACING-DECISION`** with pre-registered criteria from the plan's "Decision criteria" section. Entry shape:

```markdown
### BL-NEW-TG-PACING-DECISION: act on TG-burst-profile data after 4-week soak
**Status:** PROPOSED 2026-05-17 — filed concurrent with BL-NEW-TG-BURST-PROFILE shipping (`feat/tg-burst-profile`). Evidence-gated on the 4-week measurement window.
**Trigger:** 2026-06-14 (4 weeks post-deploy of BL-NEW-TG-BURST-PROFILE).
**Pre-registered criteria** (per `tasks/plan_tg_burst_profile.md` § Decision criteria):
- PACE if any `tg_dispatch_rejected_429` event observed in the 4-week window
- PACE if `tg_burst_observed` (group-chat callsite) >50/week sustained
- ACCEPT if zero burst OR 429 events in 4 weeks
- DM-only bursts with zero 429 → ACCEPT (1-on-1 DMs tolerate ~30/sec)
**Decision artifact:** findings doc + backlog flip to SHIPPED/ACCEPT
**decision-by:** 2026-06-14

---

## Test plan summary

- 2 config tests (default + env override)
- 8 counter-module tests:
  - single dispatch
  - two-in-1s burst
  - per-chat isolation
  - eviction after 60s
  - thread-safety (V13#2 fold: exact equality, no `_counter` import leak)
  - DM does NOT trigger 1m burst (V13 fold)
  - group chat DOES trigger 1m burst >20 (V13 fold)
  - 429 record emits rejected event (V14#2 fold)
- 4 alerter integration tests (V15 M1 fold — uses aioresponses, project idiom):
  - records when enabled
  - skips when disabled
  - 429 response captures retry_after (V14 + V15 M3)
  - instrumentation failure isolated (V15 M2)
- Full regression must pass

Total: 14 new tests.

---

## Deployment verification (autonomous post-3-reviewer-fold)

V16 fold: cron install is UNCONDITIONAL (don't gate on operator's interpretation of retention probe). Sequence:

1. **journalctl retention probe** (informational, not a gate): `ssh srilu-vps 'systemctl show systemd-journald | awk -F= "/SystemMaxRetentionUsec|SystemMaxUse/ {print}"'`. Log result for the deploy record. Archive script runs regardless.
2. **Install archive cron unconditionally** (V16 MUST-FIX #1):
   ```
   ssh srilu-vps 'mkdir -p /var/log/gecko-alpha/tg-burst-archive && chmod 0755 /var/log/gecko-alpha/tg-burst-archive'
   ssh srilu-vps 'crontab -l 2>/dev/null | grep -v tg_burst_archive | { cat; echo "30 3 * * 0 /root/gecko-alpha/scripts/tg_burst_archive.sh"; } | crontab -'
   ```
3. **Restart + verify hook fires:**
   ```
   ssh srilu-vps 'systemctl restart gecko-pipeline && sleep 5 && systemctl is-active gecko-pipeline'
   ssh srilu-vps 'journalctl -u gecko-pipeline --since "5 minutes ago" -p debug | grep "\"event\": \"tg_dispatch_observed\"" | head -3'
   ```
4. Smoke test summary: `ssh srilu-vps '/root/gecko-alpha/scripts/tg_burst_summary.sh 1'`
5. **File memory checkpoint** (V16 MUST-FIX #2): write `~/.claude/projects/.../memory/project_tg_burst_pacing_checkpoint_2026_06_14.md` with the pre-registered criteria + `tg_burst_summary.sh 672` invocation + archive dir pointer (cycle 1 pattern from `project_backlog_reaudit_checkpoint_2026_06_13.md`).
6. **Pre-registered review at 2026-06-14** per `BL-NEW-TG-PACING-DECISION` criteria.
7. Revert path: `TG_BURST_PROFILE_ENABLED=False` in `.env` + restart — disables the counter call. Full revert = revert PR.

---

## Out of scope

- DB persistence of dispatch counters — `feedback_in_memory_telemetry_persistence.md` warns that module-level counters reset on restart. Acceptable for measurement: journalctl retains structured events across restarts; aggregate via `scripts/tg_burst_summary.sh`. Persistence becomes scope only IF operator chooses to act on the data (file as follow-up at that decision).
- Active pacing / rate-limiting — explicitly NOT this PR's scope. Pacing decision is for BL-NEW-PRUNE-PACING-FOLLOWUP-equivalent follow-up after 4-week data.
- §12a watchdog on `tg_dispatch_observed` — measurement table doesn't exist; covered by the deferred §12a daemon item.
- Per-chat_id rate-limiting at the alerter layer — measurement first; intervention is a separate decision.
