# P1 #2 — Telegram send pacing (retry_after-aware) Implementation Plan

**New primitives introduced:** NONE (extends the in-tree `scout.alerter` Telegram
transport + `tg_dispatch_counter` instrumentation; adds one small observability
module `tg_pacing.py`, 2 Settings flags, no schema, no new dependency)

> **For agentic workers:** TDD task-by-task. Steps use `- [ ]` checkbox syntax.

**Goal:** Honor Telegram `retry_after` so a 429 burst is paced + retried instead
of silently dropped, and make every alert attributable by `source`.

**Architecture:** Per-chat pacing state in a small testable module
(`scout/observability/tg_pacing.py`); `send_telegram_message` gains a pre-send
pacing gate + a bounded single retry on 429; **`send_alert` is unified to route
its Telegram leg through `send_telegram_message`** so the main candidate-alert
path is paced + instrumented (today it has its own un-instrumented `session.post`);
all callsites pass an explicit `source=`. Behind Settings flags.

**Tech Stack:** Python asyncio, aiohttp, structlog, Pydantic Settings, pytest
(aioresponses + structlog capture + the `patch_module_sleep` conftest fixture).

## Hermes-first analysis (CLAUDE.md §7b)

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Telegram rate-limit pacing on a pipeline's own bot transport | none (Hermes "multi-channel response" is its own WhatsApp-agent responder) | build in-tree — routing pipeline alerts through Hermes would be drift |
| 429 parse / bounded retry | none | build in-tree (transport-level) |

awesome-hermes-agent ecosystem check: no Telegram-pacing skill. Verdict
`extends-Hermes`. Receipt: `tasks/.hermes-check-receipts/p1-2-tg-pacing.json`.

## Drift-check (CLAUDE.md §7a)

`tg_dispatch_counter.py` is **measurement-only** ("does NOT rate-limit or pace",
docstring line 2) and even pre-filed this as `BL-NEW-TG-PACING-DECISION`
(decision-by 2026-06-14). `send_telegram_message:184-202` parses `retry_after`
into `record_429` but **never acts on it**. `send_alert:288` is a SEPARATE direct
`session.post` with no instrumentation/pacing. No pacing exists. Net-new confirmed.

## Runtime re-check (§9a)

Prod 8-week archive holds exactly ONE `tg_dispatch_rejected_429` (2026-05-21,
retry_after=5, source=unattributed); none since. Low rate, but pre-registered
criterion is "ANY 429 → PACE", and the current no-honor behavior means a real
burst gets repeatedly rejected. Production sends to DM `6337722878` (positive
chat_id → Telegram tolerates ~30/s; group 20/min limit N/A).

## Gate-1 review folds (2026-06-18, operator) — SUPERSEDE the inline blocks below

**Fold 1 — send_alert unification APPROVED.** Preserve `AlertDeliveryError`,
`parse_mode="Markdown"`, and Discord behavior; test all three (delivers via paced
sender; raises AlertDeliveryError on hard failure; Discord still attempted).

**Fold 2 — don't retry early (budget gate on the retry).** Only sleep+retry when
`retry_after <= TG_PACING_MAX_WAIT_SECONDS`. If Telegram asks for MORE than the cap:
`register_429` the full paced deadline, log `tg_send_retry_skipped_over_budget`
(with retry_after + budget), and fall through to the normal non-200 path (NO early
retry — retrying after 10s on a 60s ask is predictably ineffective). Retry sleep is
the actual `retry_after` (already ≤ cap in this branch), not `min(...)`.

**Fold 3 — record EVERY actual 429.** `record_429` fires on the first 429 AND again
if the retry also returns 429 (a real Telegram rejection must stay visible in
`tg_dispatch_rejected_429`). `tg_send_retry_failed` is additive, not a replacement.

**Fold 4 — AST source-label guard, not regex.** Replace the regex test (Task 5) with
an `ast`-based test: parse each `scout/**/*.py`, find every `Call` whose func is
`send_telegram_message` (Name or Attribute), and assert it has a `source=` keyword.
Mirrors the parse-mode hygiene style; comments/formatting can't fool it.

Retry control-flow (authoritative):
```python
if status == 429:
    _record_429_safe(chat, source, retry_after)            # Fold 3: always
    if settings.TG_PACING_ENABLED:
        register_429(chat, retry_after)                    # pace future sends
        ra = float(retry_after) if retry_after and retry_after > 0 else _DEFAULT_RETRY_AFTER
        if ra <= settings.TG_PACING_MAX_WAIT_SECONDS:       # Fold 2: in-budget -> retry once
            logger.warning("tg_send_retry_after_429", chat_id=chat, source=source, retry_after=retry_after, sleep_seconds=ra)
            await asyncio.sleep(ra)
            status, body_bytes, retry_after = await _post_telegram_once(session, url, payload)
            if status == 200:
                logger.info("tg_send_retry_succeeded", chat_id=chat, source=source)
            else:
                logger.warning("tg_send_retry_failed", chat_id=chat, source=source, status=status)
                if status == 429:                          # Fold 3: retry's 429 is real
                    _record_429_safe(chat, source, retry_after)
                    register_429(chat, retry_after)
        else:                                              # Fold 2: over budget -> don't retry early
            logger.warning("tg_send_retry_skipped_over_budget", chat_id=chat, source=source, retry_after=ra, budget=settings.TG_PACING_MAX_WAIT_SECONDS)
# then fall through to the existing non-200 / 200 handling on the final status
```
(`_record_429_safe` wraps `record_429` in the existing try/except so instrumentation
failure isn't mis-attributed; gated on `TG_BURST_PROFILE_ENABLED`.)

## Global Constraints

- No hardcoded thresholds — budget from `Settings`.
- Bounded waits only: every sleep capped at `TG_PACING_MAX_WAIT_SECONDS` so a
  malicious/huge `retry_after` can't stall the pipeline.
- Preserve existing log event names (`telegram_message_delivered`, the two
  failure strings) + the `record_dispatch`/`record_429` instrumentation + the
  §12b delivered-on-200 log. ADD new pacing events only.
- Preserve `send_alert`'s `AlertDeliveryError` contract.
- Pacing state via `threading.Lock` (sync-in-async, mirroring tg_dispatch_counter).
- Tests must NOT really sleep — use `patch_module_sleep("scout.alerter")`.
- `black` formatted (run on EVERY touched file incl. tests).

## File Structure

- `scout/observability/tg_pacing.py` (new) — per-chat pacing state + pure fns.
- `scout/config.py` — +2 flags.
- `scout/alerter.py` — `_post_telegram_once` extraction + pacing gate + retry in
  `send_telegram_message`; `send_alert` routed through it.
- ~25 callsites — add `source=`.
- `tests/test_tg_pacing.py` (new), `tests/test_alerter_pacing.py` (new),
  `tests/test_alerter_source_labels.py` (new).

---

### Task 1: `tg_pacing` module

**Files:** Create `scout/observability/tg_pacing.py`; Test `tests/test_tg_pacing.py`

**Interfaces — Produces:**
- `register_429(chat_id:str, retry_after:float|None, *, now:float|None=None) -> float`
- `pacing_wait_seconds(chat_id:str, *, now:float|None=None) -> float`
- `reset_for_tests() -> None`

- [ ] **Step 1: failing tests**
```python
# tests/test_tg_pacing.py
from scout.observability.tg_pacing import (
    register_429, pacing_wait_seconds, reset_for_tests,
)

def test_register_sets_wait():
    reset_for_tests()
    register_429("c1", 5, now=1000.0)
    assert pacing_wait_seconds("c1", now=1000.0) == 5.0
    assert pacing_wait_seconds("c1", now=1003.0) == 2.0
    assert pacing_wait_seconds("c1", now=1010.0) == 0.0

def test_unpaced_chat_zero():
    reset_for_tests()
    assert pacing_wait_seconds("nope", now=1000.0) == 0.0

def test_none_retry_after_uses_default():
    reset_for_tests()
    register_429("c1", None, now=0.0)
    assert pacing_wait_seconds("c1", now=0.0) == 1.0  # default 1s

def test_keeps_later_deadline():
    reset_for_tests()
    register_429("c1", 10, now=0.0)
    register_429("c1", 2, now=0.0)  # shorter must not shrink the pacing
    assert pacing_wait_seconds("c1", now=0.0) == 10.0
```

- [ ] **Step 2: run → FAIL** (`uv run pytest tests/test_tg_pacing.py -v`).

- [ ] **Step 3: implement**
```python
"""Per-chat Telegram pacing state (P1 #2). Measurement lives in
tg_dispatch_counter; THIS module records retry_after deadlines so the sender
can wait before re-hitting a chat that Telegram just 429'd. threading.Lock
(sync-in-async) mirrors tg_dispatch_counter."""
from __future__ import annotations

import time
from threading import Lock

_DEFAULT_RETRY_AFTER = 1.0  # Telegram omits retry_after sometimes; pace 1s.
_paced_until: dict[str, float] = {}
_lock = Lock()


def register_429(chat_id: str, retry_after: float | None, *, now: float | None = None) -> float:
    now = time.monotonic() if now is None else now
    wait = float(retry_after) if retry_after and retry_after > 0 else _DEFAULT_RETRY_AFTER
    with _lock:
        deadline = max(_paced_until.get(chat_id, 0.0), now + wait)
        _paced_until[chat_id] = deadline
        return deadline


def pacing_wait_seconds(chat_id: str, *, now: float | None = None) -> float:
    now = time.monotonic() if now is None else now
    with _lock:
        until = _paced_until.get(chat_id, 0.0)
    return max(0.0, until - now)


def reset_for_tests() -> None:
    with _lock:
        _paced_until.clear()
```

- [ ] **Step 4: run → PASS**. **Step 5: commit** `feat(tg): per-chat pacing state module`.

---

### Task 2: Config flags

**Files:** Modify `scout/config.py` (near `TG_BURST_PROFILE_ENABLED`); Test `tests/test_alerter_pacing.py`

**Interfaces — Produces:** `Settings.TG_PACING_ENABLED: bool`,
`Settings.TG_PACING_MAX_WAIT_SECONDS: float`

- [ ] **Step 1: failing test**
```python
def test_pacing_flag_defaults(settings_factory):
    s = settings_factory()
    assert s.TG_PACING_ENABLED is True
    assert s.TG_PACING_MAX_WAIT_SECONDS == 10.0

def test_pacing_max_wait_rejects_nonpositive(settings_factory):
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        settings_factory(TG_PACING_MAX_WAIT_SECONDS=0)
```

- [ ] **Step 2: FAIL → Step 3: implement** (after `TG_BURST_PROFILE_ENABLED`)
```python
    # P1 #2 TG pacing: honor Telegram retry_after. Pre-send gate waits if the
    # chat is currently paced; on 429 we pace + retry once within the budget.
    TG_PACING_ENABLED: bool = True
    TG_PACING_MAX_WAIT_SECONDS: float = Field(default=10.0, gt=0)
```
- [ ] **Step 4: PASS → Step 5: commit** `feat(tg): pacing Settings flags`.

---

### Task 3: pacing gate + bounded retry in `send_telegram_message`

**Files:** Modify `scout/alerter.py`; Test `tests/test_alerter_pacing.py`

**Interfaces — Consumes:** `tg_pacing.pacing_wait_seconds/register_429`.
**Produces:** `_post_telegram_once(session, url, payload) -> tuple[int, bytes|None, int|None]`
(internal); `send_telegram_message` unchanged signature.

New structured events: `tg_pacing_wait`, `tg_send_retry_after_429`,
`tg_send_retry_succeeded`, `tg_send_retry_failed`. Existing events preserved.

- [ ] **Step 1: failing tests** (aioresponses; `patch_module_sleep("scout.alerter")`):
```python
# tests/test_alerter_pacing.py
import aiohttp
from aioresponses import aioresponses
import structlog
from scout.alerter import send_telegram_message
from scout.observability import tg_pacing

URL_RE = ... # re.compile(r"https://api\.telegram\.org/bot.*/sendMessage")

async def test_429_then_retry_succeeds(settings_factory, patch_module_sleep):
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    s = settings_factory()
    with aioresponses() as mocked:
        mocked.post(URL_RE, status=429, payload={"ok": False, "parameters": {"retry_after": 3}})
        mocked.post(URL_RE, status=200, payload={"ok": True})
        async with aiohttp.ClientSession() as sess:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message("hi", sess, s, parse_mode=None, source="t")
    ev = [e["event"] for e in logs]
    assert "tg_send_retry_after_429" in ev
    assert "tg_send_retry_succeeded" in ev
    assert "telegram_message_delivered" in ev

async def test_pre_send_waits_when_paced(settings_factory, patch_module_sleep):
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    s = settings_factory()
    tg_pacing.register_429(str(s.TELEGRAM_CHAT_ID), 5)  # chat is paced
    with aioresponses() as mocked:
        mocked.post(URL_RE, status=200, payload={"ok": True})
        async with aiohttp.ClientSession() as sess:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message("hi", sess, s, parse_mode=None, source="t")
    assert "tg_pacing_wait" in [e["event"] for e in logs]

async def test_retry_failed_logged(settings_factory, patch_module_sleep):
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    s = settings_factory()
    with aioresponses() as mocked:
        mocked.post(URL_RE, status=429, payload={"ok": False, "parameters": {"retry_after": 2}})
        mocked.post(URL_RE, status=429, payload={"ok": False, "parameters": {"retry_after": 2}})
        async with aiohttp.ClientSession() as sess:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message("hi", sess, s, parse_mode=None, source="t")
    ev = [e["event"] for e in logs]
    assert "tg_send_retry_failed" in ev

async def test_pacing_disabled_no_wait_no_retry(settings_factory, patch_module_sleep):
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    s = settings_factory(TG_PACING_ENABLED=False)
    with aioresponses() as mocked:
        mocked.post(URL_RE, status=429, payload={"ok": False, "parameters": {"retry_after": 3}})
        async with aiohttp.ClientSession() as sess:
            with structlog.testing.capture_logs() as logs:
                await send_telegram_message("hi", sess, s, parse_mode=None, source="t")
    ev = [e["event"] for e in logs]
    assert "tg_send_retry_after_429" not in ev  # disabled -> no retry
```

- [ ] **Step 2: FAIL → Step 3: implement.** Add `import asyncio`; extract
  `_post_telegram_once`; insert the pre-send gate (after `_truncate`/payload build,
  before `record_dispatch`); on 429 keep `record_429` (first occurrence), then if
  `TG_PACING_ENABLED`: `register_429`, log `tg_send_retry_after_429`,
  `await asyncio.sleep(min(retry_after or DEFAULT, TG_PACING_MAX_WAIT_SECONDS))`,
  retry `_post_telegram_once` once, log `tg_send_retry_succeeded/failed` (and
  `register_429` again if the retry is still 429). Preserve the existing
  non-200/200 logging + `raise_on_failure`. (Full code block in the build.)

- [ ] **Step 4: PASS → Step 5: commit** `feat(tg): retry_after pacing gate + bounded retry`.

---

### Task 4: unify `send_alert` through `send_telegram_message`

**Files:** Modify `scout/alerter.py`; Test `tests/test_alerter_pacing.py`

- [ ] **Step 1: failing test** — `send_alert` 429 then 200 delivers (proves it's
  now paced/retried) and `send_alert` raises `AlertDeliveryError` on hard failure.
```python
async def test_send_alert_routes_through_pacing(token_factory, settings_factory, patch_module_sleep):
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    s = settings_factory(DISCORD_WEBHOOK_URL="")
    from scout.alerter import send_alert
    with aioresponses() as mocked:
        mocked.post(URL_RE, status=429, payload={"ok": False, "parameters": {"retry_after": 2}})
        mocked.post(URL_RE, status=200, payload={"ok": True})
        async with aiohttp.ClientSession() as sess:
            with structlog.testing.capture_logs() as logs:
                await send_alert(token_factory(), ["gainers_early"], sess, s)
    src = [e.get("source") for e in logs if e.get("event") == "telegram_message_delivered"]
    assert "candidate_alert" in src

async def test_send_alert_raises_on_failure(token_factory, settings_factory, patch_module_sleep):
    import pytest
    from scout.exceptions import AlertDeliveryError
    patch_module_sleep("scout.alerter")
    tg_pacing.reset_for_tests()
    s = settings_factory(DISCORD_WEBHOOK_URL="")
    from scout.alerter import send_alert
    with aioresponses() as mocked:
        mocked.post(URL_RE, status=400, payload={"ok": False})  # hard failure, no retry
        async with aiohttp.ClientSession() as sess:
            with pytest.raises(AlertDeliveryError):
                await send_alert(token_factory(), ["x"], sess, s)
```

- [ ] **Step 2: FAIL → Step 3: implement**
```python
async def send_alert(token, signals, session, settings) -> None:
    """Send alert to Telegram (required, via the shared paced sender) + Discord."""
    message = format_alert_message(token, signals)
    try:
        await send_telegram_message(
            message, session, settings,
            parse_mode="Markdown", raise_on_failure=True, source="candidate_alert",
        )
    except Exception as exc:
        raise AlertDeliveryError(f"Telegram send failed: {exc}") from exc
    if settings.DISCORD_WEBHOOK_URL:
        try:
            async with session.post(settings.DISCORD_WEBHOOK_URL, json={"content": _truncate(message)}) as resp:
                if resp.status not in (200, 204):
                    logger.warning("Discord webhook returned error", status=resp.status)
        except Exception:
            logger.warning("Discord webhook delivery failed", exc_info=True)
```
(Verify existing `send_alert` tests still pass — they mock the Telegram URL via
aioresponses; the URL is unchanged so they should. Fix any that asserted the old
direct-post internals.)

- [ ] **Step 4: PASS → Step 5: commit** `feat(tg): route send_alert through paced sender`.

---

### Task 5: source labels on all callsites

**Files:** ~25 callsites (see grep); Test `tests/test_alerter_source_labels.py`

Add a descriptive `source=` to every `send_telegram_message` call and confirm
`send_alert` uses `candidate_alert`. Proposed labels (finalize per callsite
context during impl): `narrative_operator_alert` (api/internal_alert),
`velocity_alert` (velocity), `live_decision` (live/loops), `chain_alert`
(chains/alerts), `secondwave_alert`, `lunarcrush_social`, `cohort_digest`,
`auto_suspend` (already), `calibrate`, `weekly_digest`, `suppression`,
`tg_alert_dispatch`, `trade_surface_alert`, `narrative_agent`, plus the main.py
sites (`daily_summary`, `startup_announce`, `m1_5c_announce`, etc.).

- [ ] **Step 1: failing test** — guard against regression to unattributed on the
  hot paths:
```python
# tests/test_alerter_source_labels.py — static check that key callsites pass source=
import pathlib, re
SRC = pathlib.Path("scout")
def test_no_unlabeled_sends_in_production_paths():
    offenders = []
    for p in SRC.rglob("*.py"):
        txt = p.read_text(encoding="utf-8")
        for m in re.finditer(r"send_telegram_message\(", txt):
            seg = txt[m.start():m.start()+400]
            if "source=" not in seg:
                offenders.append(f"{p}:{txt[:m.start()].count(chr(10))+1}")
    assert offenders == [], f"unlabeled send_telegram_message: {offenders}"
```

- [ ] **Step 2: run → FAIL** (lists current unlabeled sites).
- [ ] **Step 3: add `source=` to each** until the test passes.
- [ ] **Step 4: PASS → Step 5: commit** `feat(tg): source-label all telegram sends`.

---

## Verification commands

```bash
uv run pytest tests/test_tg_pacing.py tests/test_alerter_pacing.py \
  tests/test_alerter_source_labels.py tests/test_alerter_tg_burst_hook.py -v
uv run black --check scout/alerter.py scout/config.py \
  scout/observability/tg_pacing.py tests/test_tg_pacing.py \
  tests/test_alerter_pacing.py tests/test_alerter_source_labels.py
uv run pytest --tb=short -q   # full suite (CI/Linux; Windows aiohttp import → OPENSSL)
```

## Risks / open design questions for gate-1 review

1. **send_alert unification (the big one):** routing `send_alert`'s Telegram leg
   through `send_telegram_message` changes the main candidate-alert path — it now
   records dispatch, applies pacing, and raises `RuntimeError`→wrapped as
   `AlertDeliveryError`. This is required (otherwise the busiest alert path isn't
   paced) but it's production-critical. Confirm acceptable.
2. **Single retry vs N:** scope is ONE bounded retry (per your spec). A persistent
   429 falls through to the normal non-200 path (logged/raised); the next call is
   pre-gated by the pacing deadline. OK?
3. **record_429 on retry:** fire `record_429` (the measurement event) only on the
   FIRST 429; the retry's outcome is `tg_send_retry_succeeded/failed`. OK?
4. **Windows tests:** alerter tests import aiohttp → OPENSSL on Windows; rely on
   CI/Linux (the `tg_pacing` + config + source-label-static tests run locally).
5. **Source-label test** is a static grep guard — coarse but prevents regressions.

## Review section (fill after implementation)

- _Diff summary / test results / residual risks:_
