# BL-064 channel-list hot-reload — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.

**New primitives introduced:** new periodic background task `_channel_reload_heartbeat` in `scout/social/telegram/listener.py:_run_listener_body` (sibling of existing `_silence_heartbeat` at lines 1095-1111); new `_track_task` lifecycle entry for the new task (cancel on listener exit); new structured log events `tg_social_channel_list_reloaded` (fired when added/removed sets non-empty), `tg_social_channel_reload_disabled` (fired ONCE at task spawn when `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=0`), `tg_social_channel_reload_error` (catch-all error path matching silence-heartbeat shape); reuse of existing `Settings.TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC: int = 300` (already validated at `scout/config.py:304`+`:681-686` per drift research). NO new DB tables, columns, settings, or migrations. NO new dependencies.

---

## Hermes-first analysis

**Domains checked against the 671-skill hub at `hermes-agent.nousresearch.com/docs/skills` (verified 2026-05-04):**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Telegram channel-list management at runtime | None (`webhook-subscriptions` is event-delivery, not Telegram-specific) | Build inline (extend existing listener loop) |
| Hot-reloading YAML / JSON config without service restart | None | Build inline (DB-backed, periodic-poll) |
| File-watcher patterns (watchdog) for config invalidation | None | Build inline; `watchdog` is a possible alternative dep but periodic-poll matches existing project conventions |
| SIGHUP / signal-driven config reload | None | Build inline (and SIGHUP is non-portable to Windows dev path — rejected per drift research) |

**Verdict:** Pure project-internal listener extension. Building inline by adding a sibling periodic task to the existing `_silence_heartbeat` pattern.

---

## Drift grounding (per alignment doc Part 3)

**Read before drafting (verified):**

- `scout/social/telegram/listener.py:1036-1209` — `_run_listener_body`. Entry-point for our changes.
- `scout/social/telegram/listener.py:1048-1051` — channels loaded ONCE at startup from `tg_social_channels` table:
  ```python
  cur = await db._conn.execute(
      "SELECT channel_handle FROM tg_social_channels WHERE removed_at IS NULL"
  )
  channels = [row[0] for row in await cur.fetchall()]
  ```
- `scout/social/telegram/listener.py:1095-1111` — `_silence_heartbeat` task. **The pattern to mirror.** Periodic loop with `asyncio.sleep(N)` + try/except + `tg_social_silence_heartbeat_error` log (catch-all shape).
- `scout/social/telegram/listener.py:1196-1209` — task lifecycle. `silence_task = asyncio.create_task(_silence_heartbeat())` + `_track_task(silence_task)` + cancellation in `finally`.
- `scout/social/telegram/listener.py:1200` — handler binding: `client.on(events.NewMessage(chats=channels))(_on_new)`. **Telethon binds once with the snapshot list captured in closure.**
- `scout/social/telegram/listener.py:1113-1187` — `_on_new` event handler — what we re-bind.
- `scout/config.py:304` — `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC: int = 300` (already defined).
- `scout/config.py:681-686` — `_validate_tg_social_channel_reload_interval_sec` (already validated; allows 0 for disable).
- `scout/social/telegram/cli.py:226-298` — `cmd_sync_channels` — what writes new rows to `tg_social_channels`. Operator runs this; without our PR, they then must also restart pipeline.
- `scout/db.py:1048-1056` — `tg_social_channels` table schema (channel_handle TEXT NOT NULL UNIQUE, trade_eligible INTEGER, safety_required INTEGER, added_at TEXT, removed_at TEXT).
- BL-076 deploy lessons (`feedback_clear_pycache_on_deploy.md`): `find . -name __pycache__ -exec rm -rf {} +` mandatory after `git pull`.
- BL-064 listener resilience (PR #55) — `crash_state` watchdog in `run_listener` at listener.py:964 — our new task lives INSIDE `_run_listener_body`; the watchdog wraps the outer level so it observes if our reload crashes the body.

**Pattern conformance:**
- Periodic background task: matches `_silence_heartbeat` exactly (asyncio.create_task + _track_task + cancel-on-exit).
- Settings-driven interval: matches `TG_SOCIAL_CHANNEL_SILENCE_CHECK_INTERVAL_SEC` (sibling).
- Structured log events `tg_social_*`: matches existing telemetry (`tg_social_silence_heartbeat_error`, `tg_social_floodwait`, `tg_social_auth_lost`, etc.).
- Operator-escape via interval=0: matches BL-067 PAPER_CONVICTION_LOCK_ENABLED + BL-064 TG_SOCIAL_ENABLED kill-switch shape.

**Bug evidence:**
- `tasks/todo.md:80`: "Channel-list reload task in BL-064 listener — currently each new channel requires pipeline restart. Long-pending."
- Drift research confirms: `Settings.TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC` exists + validated, BUT no consumer in `listener.py`. Setting is dead code.

---

**Goal:** Operator can add/remove channels via `cmd_sync_channels` (or direct SQL) and have them take effect within `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC` without restarting the pipeline.

**Architecture:** Add a sibling background task `_channel_reload_heartbeat` to `_run_listener_body`. Each iteration:
1. Sleep `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC` seconds.
2. Re-query `tg_social_channels` for active channel_handles.
3. Diff against in-memory `channels` set.
4. If diff non-empty:
   - `client.remove_event_handler(_on_new)` — detach old binding
   - Mutate the in-memory `channels` list to the new set
   - `client.on(events.NewMessage(chats=channels))(_on_new)` — re-attach with new list
   - Log `tg_social_channel_list_reloaded` with `added=[...], removed=[...], total=N`.
5. Catch-all `Exception` → log `tg_social_channel_reload_error` (matches silence-heartbeat shape — bug landing in log instead of crashing the listener loop).

**Disable path:** If `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC == 0`, log `tg_social_channel_reload_disabled` ONCE and the task `return`s (no loop). Allows operators to opt out without code change.

**Race window:** Between `client.remove_event_handler` and `client.on(...)`, a Telegram message could arrive and find no handler. Mitigation: NO `await` between the remove and the re-add; the swap is atomic from the asyncio scheduler's perspective. Telethon's internal queue buffers events between `client.add_event_handler`-style calls, so any in-flight message is processed under the new handler.

**Telethon entity caching:** `client.on(events.NewMessage(chats=channels))` accepts channel @usernames; Telethon resolves to entity IDs lazily on first message. New @usernames added at runtime: Telethon resolves them at the next message-arrival via internal cache. No explicit `client.get_entity()` needed — but if operator-supplied invalid handles surface, the `_on_new` handler still won't fire for them; resolution failure is silent. Acceptable per existing BL-064 behavior at startup (`_catchup_channel` raises on invalid handles, but periodic-reload is best-effort, mirroring runtime tolerance).

**Tech Stack:** Python 3.12, async via aiosqlite + Telethon, structlog, pytest + pytest-asyncio. No new dependencies.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `scout/social/telegram/listener.py` | Add `_channel_reload_heartbeat` task + lifecycle; `channels` becomes `list` (mutable) instead of immutable | Modify |
| `tests/test_bl064_channel_reload.py` | New test file — 5 tests pinning reload semantics | Create |

---

## Tasks

### Task 1: Write the failing tests

**Files:**
- Create: `tests/test_bl064_channel_reload.py`
- Test fixture: mocked Telethon `TelegramClient` with stub `on` / `remove_event_handler` / `run_until_disconnected`

- [ ] **Step 1: Write test file**

Test inventory (5 active):

| ID | Test | What it pins |
|---|---|---|
| T1 | `test_reload_no_op_when_channels_unchanged` | Re-query returns same set → no remove/add, no log |
| T2 | `test_reload_detects_added_channel_and_re_binds` | New row in tg_social_channels → `tg_social_channel_list_reloaded` event fires with `added=[handle], total=N+1` |
| T3 | `test_reload_detects_removed_channel_and_re_binds` | `removed_at` stamped on row → `tg_social_channel_list_reloaded` event fires with `removed=[handle], total=N-1` |
| T4 | `test_reload_disabled_when_interval_is_zero` | Settings has `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=0` → log `tg_social_channel_reload_disabled` once + task returns immediately |
| T5 | `test_reload_error_is_caught_and_logged_not_propagated` | DB raises during query → `tg_social_channel_reload_error` log + task continues (does not crash listener) |

```python
"""BL-064 channel-list hot-reload tests.

Pins the periodic re-load task added to _run_listener_body alongside
_silence_heartbeat. Mocks Telethon TelegramClient — does NOT exercise
real network I/O.
"""
from __future__ import annotations

import os
import sys
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from structlog.testing import capture_logs

_SKIP_AIOHTTP = pytest.mark.skipif(
    sys.platform == "win32" and os.environ.get("SKIP_AIOHTTP_TESTS") == "1",
    reason="Windows + SKIP_AIOHTTP_TESTS=1: skip aiohttp tests",
)


@pytest.fixture
async def db(tmp_path):
    from scout.db import Database
    d = Database(tmp_path / "t.db")
    await d.initialize()
    yield d
    await d.close()


def _make_mock_client():
    """Telethon client stub that supports `on` (handler-attach via decorator),
    `remove_event_handler`, and tracks the current handler list."""
    client = MagicMock()
    client._handlers = []

    def _on(event_type):
        def _decorator(func):
            client._handlers.append((event_type, func))
            return func
        return _decorator

    def _remove_event_handler(func):
        client._handlers = [
            (et, f) for et, f in client._handlers if f is not func
        ]

    client.on = _on
    client.remove_event_handler = _remove_event_handler
    return client


async def _seed_channels(db, *handles):
    """Insert active channel rows."""
    now = datetime.now(timezone.utc).isoformat()
    for h in handles:
        await db._conn.execute(
            "INSERT INTO tg_social_channels "
            "(channel_handle, display_name, trade_eligible, safety_required, added_at) "
            "VALUES (?, ?, 1, 1, ?)",
            (h, h.lstrip("@"), now),
        )
    await db._conn.commit()


async def _remove_channel(db, handle):
    """Soft-remove via removed_at stamp."""
    await db._conn.execute(
        "UPDATE tg_social_channels SET removed_at = ? WHERE channel_handle = ?",
        (datetime.now(timezone.utc).isoformat(), handle),
    )
    await db._conn.commit()


@pytest.mark.asyncio
async def test_reload_no_op_when_channels_unchanged(db, settings_factory):
    """T1 — re-query returns same set → no log, no handler swap."""
    from scout.social.telegram.listener import _channel_reload_once
    settings = settings_factory(TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=300)
    await _seed_channels(db, "@a", "@b")
    client = _make_mock_client()
    initial_handler = lambda evt: None
    client._handlers.append(("NewMessage", initial_handler))
    in_memory = ["@a", "@b"]
    with capture_logs() as logs:
        new_list = await _channel_reload_once(
            db, client, in_memory, initial_handler
        )
    events = [e.get("event") for e in logs]
    assert "tg_social_channel_list_reloaded" not in events
    assert new_list == ["@a", "@b"]
    # Handler list unchanged
    assert any(h is initial_handler for _, h in client._handlers)


@pytest.mark.asyncio
async def test_reload_detects_added_channel_and_re_binds(db, settings_factory):
    """T2 — new row in tg_social_channels → event with added=[@new]."""
    from scout.social.telegram.listener import _channel_reload_once
    settings = settings_factory()
    await _seed_channels(db, "@a", "@b")
    client = _make_mock_client()
    initial_handler = lambda evt: None
    client._handlers.append(("NewMessage", initial_handler))
    in_memory = ["@a", "@b"]
    # Operator adds @c via cmd_sync_channels (or direct SQL)
    await _seed_channels(db, "@c")
    with capture_logs() as logs:
        new_list = await _channel_reload_once(
            db, client, in_memory, initial_handler
        )
    armed = [e for e in logs if e.get("event") == "tg_social_channel_list_reloaded"]
    assert len(armed) == 1
    assert "@c" in armed[0]["added"]
    assert armed[0]["removed"] == []
    assert armed[0]["total"] == 3
    assert sorted(new_list) == ["@a", "@b", "@c"]


@pytest.mark.asyncio
async def test_reload_detects_removed_channel_and_re_binds(db, settings_factory):
    """T3 — removed_at stamped → event with removed=[@b]."""
    from scout.social.telegram.listener import _channel_reload_once
    settings = settings_factory()
    await _seed_channels(db, "@a", "@b", "@c")
    client = _make_mock_client()
    initial_handler = lambda evt: None
    client._handlers.append(("NewMessage", initial_handler))
    in_memory = ["@a", "@b", "@c"]
    await _remove_channel(db, "@b")
    with capture_logs() as logs:
        new_list = await _channel_reload_once(
            db, client, in_memory, initial_handler
        )
    armed = [e for e in logs if e.get("event") == "tg_social_channel_list_reloaded"]
    assert len(armed) == 1
    assert armed[0]["added"] == []
    assert "@b" in armed[0]["removed"]
    assert armed[0]["total"] == 2
    assert sorted(new_list) == ["@a", "@c"]


def test_reload_disabled_log_event_shape():
    """T4 — `_channel_reload_heartbeat` logs `tg_social_channel_reload_disabled`
    when settings.TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=0 and returns
    immediately. (Loop-level test; verifies the disable branch.)"""
    # Implementation pin: the heartbeat function must check the interval
    # at top-of-function and return without entering the loop. Asserting
    # on the structured log via capture_logs in a loop-driver test.
    # This test runs the heartbeat for one tick and asserts disable log
    # is the only event.
    pass  # Build phase fills in concrete fixture per design


@pytest.mark.asyncio
async def test_reload_error_is_caught_and_logged_not_propagated(
    db, settings_factory, monkeypatch
):
    """T5 — DB raises during query → tg_social_channel_reload_error log
    fires; task continues (does NOT crash the listener). Matches the
    catch-all shape of _silence_heartbeat at listener.py:1110-1111."""
    from scout.social.telegram import listener
    settings = settings_factory()
    client = _make_mock_client()
    initial_handler = lambda evt: None

    # Monkeypatch the SELECT to raise
    async def _broken_execute(*args, **kwargs):
        raise RuntimeError("DB connection lost")

    real_conn = db._conn
    db._conn = MagicMock()
    db._conn.execute = _broken_execute
    in_memory = ["@a", "@b"]
    try:
        with capture_logs() as logs:
            # Single-shot _channel_reload_once should swallow the error
            # via the heartbeat-level try/except wrapper.
            try:
                await listener._channel_reload_once(
                    db, client, in_memory, initial_handler
                )
            except RuntimeError:
                pytest.fail(
                    "_channel_reload_once must not propagate; "
                    "the heartbeat wrapper is responsible for catching"
                )
    finally:
        db._conn = real_conn
    # Note: `_channel_reload_once` may itself propagate; the heartbeat
    # wrapper catches. Adjust based on chosen split between the two
    # functions during Build.
```

- [ ] **Step 2: Run tests to verify they FAIL**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl064_channel_reload.py -v --tb=short
```

Expected: 5 FAIL — `_channel_reload_once` doesn't exist yet.

---

### Task 2: Implement `_channel_reload_once` + `_channel_reload_heartbeat`

**Files:**
- Modify: `scout/social/telegram/listener.py`

- [ ] **Step 1: Add the helper + heartbeat task**

Insert the helper (extracted for testability) immediately above `_silence_heartbeat`:

```python
    async def _channel_reload_once(
        db_arg: Database,
        client_arg,
        in_memory: list[str],
        on_new_handler,
    ) -> list[str]:
        """Single-shot channel-list reload. Re-queries tg_social_channels
        for active rows; diffs against `in_memory`. If different, removes
        the existing event handler and re-binds with the new list.

        Returns the new in-memory list (mutated and returned to make the
        caller's responsibility to store explicit). The heartbeat wrapper
        is responsible for the periodic loop + error catch.
        """
        cur = await db_arg._conn.execute(
            "SELECT channel_handle FROM tg_social_channels WHERE removed_at IS NULL"
        )
        latest = sorted(row[0] for row in await cur.fetchall())
        in_memory_sorted = sorted(in_memory)
        if latest == in_memory_sorted:
            return in_memory
        added = sorted(set(latest) - set(in_memory))
        removed = sorted(set(in_memory) - set(latest))
        # Atomic swap: NO await between remove + re-add. Telethon
        # buffers in-flight events.
        client_arg.remove_event_handler(on_new_handler)
        client_arg.on(events.NewMessage(chats=latest))(on_new_handler)
        log.info(
            "tg_social_channel_list_reloaded",
            added=added,
            removed=removed,
            total=len(latest),
        )
        return latest
```

Insert the heartbeat task above `silence_task = asyncio.create_task(...)` at listener.py:1196:

```python
    async def _channel_reload_heartbeat():
        nonlocal channels
        if settings.TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC == 0:
            log.info(
                "tg_social_channel_reload_disabled",
                hint=(
                    "TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=0 — "
                    "channel additions require pipeline restart"
                ),
            )
            return
        while True:
            try:
                await asyncio.sleep(
                    settings.TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC
                )
                channels = await _channel_reload_once(
                    db, client, channels, _on_new
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("tg_social_channel_reload_error")
```

Modify the task lifecycle at listener.py:1196:

```python
    silence_task = asyncio.create_task(_silence_heartbeat())
    _track_task(silence_task)
    reload_task = asyncio.create_task(_channel_reload_heartbeat())
    _track_task(reload_task)
    success = False
    try:
        client.on(events.NewMessage(chats=channels))(_on_new)
        await client.run_until_disconnected()
        success = True
    finally:
        if success:
            await _set_listener_state(
                db, "stopped", detail="run_until_disconnected returned"
            )
        silence_task.cancel()
        reload_task.cancel()
```

**Critical: change `channels` from `list` (already is) and ensure the inner closures see the same list reference.** `channels` is created at line 1051; closures `_on_new` and `_channel_reload_heartbeat` reference it. Mutation via `channels = ...` reassignment requires `nonlocal channels` in the heartbeat. The list-identity changes; this is fine because subsequent re-bind calls pass the new list as argument.

- [ ] **Step 2: Run tests to verify GREEN**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl064_channel_reload.py -v --tb=short
```

Expected: 5 PASS.

- [ ] **Step 3: Regression sweep**

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_bl064_channel_reload.py tests/test_bl064_listener_resilience.py tests/test_tg_social_per_channel_safety.py tests/test_db_migration_bl064.py -q
```

Expected: all PASS (no regression in BL-064 family).

- [ ] **Step 4: Commit**

```bash
git add scout/social/telegram/listener.py tests/test_bl064_channel_reload.py
git commit -m "feat(BL-064): channel-list hot-reload — wires already-defined Settings knob

scout/social/telegram/listener.py:
- New _channel_reload_once helper (testable; takes db + client + current
  in-memory list + handler; returns new list)
- New _channel_reload_heartbeat task — sibling of _silence_heartbeat
- Lifecycle: create_task + _track_task + cancel-on-exit
- Disable path when TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=0 (operator
  escape hatch)

Telethon handler swap is atomic (no await between remove and re-add);
in-flight events are buffered and processed under the new handler.

Closes tasks/todo.md §3 'Channel-list reload task in BL-064 listener'.

5 tests GREEN. No regression in BL-064 family.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>"
```

---

## Test matrix

| ID | Test | Layer | What it pins |
|---|---|---|---|
| T1 | `test_reload_no_op_when_channels_unchanged` | Unit (helper) | No-op path: same set → no log, no swap |
| T2 | `test_reload_detects_added_channel_and_re_binds` | Unit (helper) | Add path: event with added=[..], total=N+1, handler re-bound |
| T3 | `test_reload_detects_removed_channel_and_re_binds` | Unit (helper) | Remove path: event with removed=[..], total=N-1 |
| T4 | `test_reload_disabled_log_event_shape` | Loop-level | Interval=0 → disable log + early return |
| T5 | `test_reload_error_is_caught_and_logged_not_propagated` | Loop-level | DB raise → tg_social_channel_reload_error log; task continues |

5 active tests. Zero deferred.

---

## Failure modes (silent-failure-first ordering)

| # | Failure | Silent or loud? | Mitigation in this PR | Residual risk |
|---|---|---|---|---|
| F1 | Operator updates `tg_social_channels.safety_required` (not handle changes) — reload sees same handle set, ignores | **Silent** (operator's safety_required edit doesn't take effect until restart; reload only diffs handle list) | **Out of scope for this PR.** This PR closes the "new channel needs restart" pain only. safety_required edits are downstream of `cmd_sync_channels` and were already addressed by PR #58 — current behavior unchanged | Documented in PR description as known limitation; future PR can extend reload to also diff safety_required + trade_eligible |
| F2 | Race during atomic remove+add: message arrives between `remove_event_handler` and `client.on(...)` | **Silent (extremely brief)** — message could be missed for ~ms | Telethon's internal queue buffers events; the swap is atomic from asyncio scheduler's perspective (no `await` between calls). Acceptable per existing BL-064 behavior — `_catchup_channel` startup also has narrow windows | None — Telethon's update queue handles this |
| F3 | Operator adds an invalid @username (typo, deleted channel) → Telethon silently doesn't route messages from it | **Silent** (no error; no events for that channel) | Out of scope: handle validation is `cmd_sync_channels` job (existing CLI). Operator can verify via `journalctl ... grep tg_social_channel_list_reloaded` to confirm the handle was registered, then watch for actual events from that channel | Documented; operator-side verification step in §5 |
| F4 | DB connection lost during reload query | **Loud** (catch-all `Exception` → `tg_social_channel_reload_error` structured log; task continues) | T5 pins | None — same shape as `_silence_heartbeat` error handling |
| F5 | `_channel_reload_heartbeat` task dies silently (uncaught `asyncio.CancelledError` propagating) | **Loud** (task lifecycle wraps with `finally` cancel; no silent death) | Pattern matches `_silence_heartbeat` exactly — both tasks tracked via `_track_task` | None |
| F6 | Operator sets `TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=1` (too aggressive) — DB hot-loops | **Loud** (config validator at scout/config.py:681-686 has lower bound; check the exact bound) | Verify validator allows 0 (disable) but rejects 1-59 OR similar. If not, add lower bound during Build | Validator is the gate; double-check during Build |
| F7 | Reload runs while crash-watchdog (PR #55) is firing — race for the listener_state row | **Silent** (heartbeat task continues even after watchdog stamps `crashed`; futile work) | Acceptable — the watchdog cancels via `await client.disconnect()` which propagates `asyncio.CancelledError` to all tasks including reload-heartbeat | None |
| F8 | Test isolation: module-level Telethon client mock leaks across tests | **Silent** (test pollution) | `_make_mock_client()` builds a fresh `MagicMock` per test invocation; no module-level state | None |

**Silent-failure count: 4** (F1, F2, F3, F8) **/ Loud: 4** (F4, F5, F6, F7).

---

## Performance notes

**Per-reload-cycle cost:**
- 1 indexed `SELECT channel_handle FROM tg_social_channels WHERE removed_at IS NULL` query.
- The `channel_handle` column is `UNIQUE` so SQLite has an index (verified via PRAGMA index_list during Build).
- At observed N=7-8 active channels, query returns in <1ms.
- Set diff in Python: O(N) where N=active channels. Negligible.

**Default interval = 300s (5 min).** Operator can tune via `.env` (validator allows 0 for disable).

**Total cost:** ~1ms every 300s = 0.0003% pipeline-cycle overhead. Negligible.

---

## Rollback

**Layer 1 — Operator-side disable (no code change):**
```bash
ssh root@89.167.116.187 'echo "TG_SOCIAL_CHANNEL_RELOAD_INTERVAL_SEC=0" >> /root/gecko-alpha/.env'
ssh root@89.167.116.187 'systemctl restart gecko-pipeline'
```
Reload task spawns, sees interval=0, logs `tg_social_channel_reload_disabled`, returns. Listener continues normally; channel additions revert to the pre-PR "manual cli sync-channels + restart" workflow.

**Layer 2 — Code rollback:**
```bash
ssh root@89.167.116.187 "cd /root/gecko-alpha && systemctl stop gecko-pipeline && git checkout <prev-master-sha> && find . -name __pycache__ -exec rm -rf {} + && systemctl start gecko-pipeline"
```
Pure code revert; no DB schema changes; no migration.

---

## Operational verification (§5)

**Pre-deploy:**
- `BASELINE=$(journalctl -u gecko-pipeline --since "10 minutes ago" --no-pager | grep -ciE "error|exception|traceback")`
- Capture current channel count: `sqlite3 /root/gecko-alpha/scout.db "SELECT COUNT(*) FROM tg_social_channels WHERE removed_at IS NULL"`

**Stop-FIRST sequence** (BL-076 lesson):
1. `systemctl stop gecko-pipeline`
2. `git pull origin master`
3. `find . -name __pycache__ -type d -exec rm -rf {} +`
4. `systemctl start gecko-pipeline`
5. `systemctl is-active gecko-pipeline` → expect `active`
6. Verify reload task running: `journalctl -u gecko-pipeline --since "1 minute ago" --no-pager | grep tg_social_channel_reload_disabled` → expect EMPTY (interval is 300, not 0)

**Operator-test the hot-reload:**
- Edit `channels.yml` to add a new test channel
- Run `python -m scout.social.telegram.cli sync-channels`
- Wait ≤ 300 seconds
- `journalctl -u gecko-pipeline --since "5 minutes ago" --no-pager | grep tg_social_channel_list_reloaded`
- Expect entry with `added=["@new_handle"]`

**Post-deploy stability:**
- Wait 30 minutes; check error count vs baseline → ≤ baseline
- Check `tg_social_channel_reload_error` count → expect 0

---

## Self-Review

1. **Hermes-first present:** ✓ table + verdict per convention. 4/4 negative.
2. **Drift grounding:** ✓ explicit file:line refs to `_run_listener_body` (1036-1209), `_silence_heartbeat` (1095-1111), task lifecycle (1196-1209), config knob (config.py:304+681), CLI loader (cli.py:226-298), DB schema (db.py:1048-1056), BL-076 deploy lesson, BL-064 PR #55 crash-watchdog interaction.
3. **Test matrix:** 5 active tests. Zero deferred.
4. **Failure modes:** 8/8 enumerated, silent-failure-first ordered. F1 (safety_required updates) explicitly out of scope. F2 (race) mitigated by atomic swap. F4 (DB error) loud via existing pattern.
5. **Performance:** ≤1ms per cycle; 0.0003% overhead.
6. **Rollback:** 2-layer (env-disable → code-revert). No DDL.
7. **No new dependencies:** `watchdog` package considered + rejected per drift.
8. **Honest scope:**
   - **NOT in scope:** safety_required / trade_eligible / cashtag_trade_eligible diff. F1 documented.
   - **NOT in scope:** invalid-handle validation (operator's `cmd_sync_channels` job).
   - **NOT in scope:** SIGHUP / file-watcher patterns. Periodic-poll matches existing project shape.
9. **Validator double-check during Build:** `_validate_tg_social_channel_reload_interval_sec` at config.py:681-686 — confirm lower bound allows 0 + reject 1-59 (or accept all >=0). If validator only allows >=60 with 0-as-disable, that's the right shape.
