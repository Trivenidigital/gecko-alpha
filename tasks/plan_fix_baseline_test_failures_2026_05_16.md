**New primitives introduced:** NONE

# Plan: Fix baseline test failures surfaced by PR #136 review

> **For agentic workers:** Use superpowers:executing-plans for inline execution, or superpowers:subagent-driven-development if splitting by failure cluster. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the current 17-failure baseline subset green without weakening production safety checks or hiding real regressions.

**Architecture:** Fix root causes by cluster. Most failures are stale tests or env-coupled fixtures; two failures should be resolved in production code by pinning Telegram `parse_mode=None` on unsafe/default call sites and preserving documented fail-closed behavior for narrative resolution errors.

**Tech Stack:** pytest, pytest-asyncio, aioresponses, Pydantic Settings, aiosqlite, structlog.

---

## Reproduction

Run the currently red subset:

```powershell
uv run pytest --tb=short -q `
  tests/test_bl064_channel_reload.py::test_reload_disabled_when_interval_is_zero `
  tests/test_bl076_junk_filter_and_symbol_name.py::test_open_trade_logs_warning_when_symbol_and_name_both_empty `
  tests/test_bl076_junk_filter_and_symbol_name.py::test_open_trade_warning_fires_even_during_warmup `
  tests/test_bl076_junk_filter_and_symbol_name.py::test_trade_volume_spikes_passes_symbol_and_name_to_engine `
  tests/test_bl076_junk_filter_and_symbol_name.py::test_trade_predictions_passes_symbol_and_name_to_engine `
  tests/test_bl076_junk_filter_and_symbol_name.py::test_chain_completed_orphan_does_not_trigger_engine_warning `
  tests/test_bl076_junk_filter_and_symbol_name.py::test_open_trade_with_expected_empty_metadata_suppresses_warnings `
  tests/test_bl076_junk_filter_and_symbol_name.py::test_trade_chain_completions_uses_lookup_helper_for_metadata `
  tests/test_bl076_junk_filter_and_symbol_name.py::test_trade_chain_completions_falls_back_to_empty_when_no_snapshot `
  tests/test_calibration_dryrun_scheduler.py::test_calibration_dryrun_scheduler_happy_path_fires_alert `
  tests/test_calibration_dryrun_scheduler.py::test_calibration_dryrun_scheduler_idempotency `
  tests/test_heartbeat_mcap_missing.py::test_fetch_top_movers_increments_counter `
  tests/test_heartbeat_mcap_missing.py::test_fetch_by_volume_increments_counter `
  tests/test_narrative_prediction_token_id.py::test_resolution_check_error_fails_closed `
  tests/test_parse_mode_hygiene.py::test_all_dispatch_sites_pin_parse_mode `
  tests/test_signal_params_auto_suspend.py::test_revive_signal_with_baseline_stamps_baseline_and_audit `
  tests/test_signal_params_auto_suspend.py::test_revive_signal_with_baseline_on_already_enabled_signal
```

Observed on 2026-05-16: `17 failed, 5 warnings in 12.19s`.

---

## Root-Cause Summary

| Cluster | Failing tests | Root cause | Fix shape |
|---|---:|---|---|
| BL-064 reload disable | 1 | Test expects interval=0 heartbeat to return immediately, but current implementation intentionally logs once and sleeps/re-logs hourly. | Update test to start heartbeat task, wait for first log, then cancel and assert `CancelledError` propagates. |
| Settings env coupling | 8 | `tests/test_bl076_junk_filter_and_symbol_name.py` calls bare `Settings()`, so missing local `.env` fails required fields and VPS `.env` can also override defaults. | Use `settings_factory()` in the affected tests and make the shared fixture pass `_env_file=None` to isolate unit tests from operator `.env`. |
| Calibration dry-run mocks | 2 | Tests monkeypatch `send_telegram_message` with fake functions that do not accept the production `parse_mode=None` kwarg; scheduler catches the resulting `TypeError` and records no sent message. | Change fake send functions to accept `**kwargs` and assert `parse_mode is None` where relevant. |
| Heartbeat mcap HTTP mocks | 2 | `aioresponses` registers the bare CoinGecko URL while implementation calls with query params; requests miss the mock and try real network, so counters stay zero. | Use the existing regex pattern approach from `tests/test_coingecko.py` or register expected query URLs for all pages. Regex is preferred. |
| Narrative resolution fail-closed | 1 | Test monkeypatch raises generic `RuntimeError`, but current code only converts documented `DbNotInitializedError` / `CoinIdResolutionError` into fail-closed skips. Test intent says "DB outage"; it should raise the documented DB-resolution exception. | Update test monkeypatch to raise `CoinIdResolutionError`. Do not broaden production catch to all `RuntimeError`, because that would undo the narrowed-exception discipline. |
| Parse-mode hygiene | 1 | Existing allowlist line numbers for `scout/main.py` drifted after main.py edits; the deeper issue is that four call sites still rely on the default Markdown parser. | Prefer production fix: add `parse_mode=None` at combo refresh, briefing chunks, counter follow-up, and daily summary calls instead of chasing line-number allowlist churn. |
| Revival audit ordering | 2 | `revive_signal_with_baseline` now writes both `enabled` and `tg_alert_eligible` audit rows with the same timestamp; tests query latest row without filtering `field_name`, so they sometimes read the joint tg row. | Filter audit assertions by `field_name='enabled'`; add/keep separate assertion that the tg eligibility audit row exists. |

---

## Task 1: Isolate Settings in test fixtures and BL-076 tests

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/test_bl076_junk_filter_and_symbol_name.py`

- [ ] **Step 1: Make `settings_factory` ignore operator `.env`**

Change `tests/conftest.py`:

```python
@pytest.fixture
def settings_factory():
    def _make(**overrides):
        defaults = dict(
            _env_file=None,
            TELEGRAM_BOT_TOKEN="t",
            TELEGRAM_CHAT_ID="c",
            ANTHROPIC_API_KEY="k",
        )
        defaults.update(overrides)
        return Settings(**defaults)

    return _make
```

- [ ] **Step 2: Convert BL-076 direct `Settings()` calls**

In `tests/test_bl076_junk_filter_and_symbol_name.py`, add `settings_factory` to each failing test signature and replace `Settings()` with `settings_factory()`.

Affected tests:

```text
test_open_trade_logs_warning_when_symbol_and_name_both_empty
test_open_trade_warning_fires_even_during_warmup
test_trade_volume_spikes_passes_symbol_and_name_to_engine
test_trade_predictions_passes_symbol_and_name_to_engine
test_chain_completed_orphan_does_not_trigger_engine_warning
test_open_trade_with_expected_empty_metadata_suppresses_warnings
test_trade_chain_completions_uses_lookup_helper_for_metadata
test_trade_chain_completions_falls_back_to_empty_when_no_snapshot
```

- [ ] **Step 3: Verify**

Run:

```powershell
uv run pytest --tb=short -q tests/test_bl076_junk_filter_and_symbol_name.py
```

Expected: BL-076 file passes; aiosqlite thread warnings should disappear because tests will reach their `await sd.close()` cleanup paths.

---

## Task 2: Update BL-064 interval=0 heartbeat test

**Files:**
- Modify: `tests/test_bl064_channel_reload.py`

- [ ] **Step 1: Replace immediate-return expectation**

Current implementation at `scout/social/telegram/listener.py` logs `tg_social_channel_reload_disabled`, then sleeps for one hour so the disabled state is periodically visible. Update the test body to:

```python
with capture_logs() as logs:
    task = asyncio.create_task(heartbeat())
    try:
        for _ in range(20):
            if any(e.get("event") == "tg_social_channel_reload_disabled" for e in logs):
                break
            await asyncio.sleep(0.01)
        events = [e.get("event") for e in logs]
        assert "tg_social_channel_reload_disabled" in events
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
```

- [ ] **Step 2: Verify**

Run:

```powershell
uv run pytest --tb=short -q tests/test_bl064_channel_reload.py::test_reload_disabled_when_interval_is_zero
```

Expected: pass.

---

## Task 3: Fix calibration dry-run test fakes

**Files:**
- Modify: `tests/test_calibration_dryrun_scheduler.py`

- [ ] **Step 1: Allow production kwargs in fake senders**

Change both failing fake senders:

```python
async def _fake_send(msg, session, settings, **kwargs):
    sent_messages.append(msg)
```

For the happy-path test, also capture kwargs:

```python
sent_kwargs = []

async def _fake_send(msg, session, settings, **kwargs):
    sent_messages.append(msg)
    sent_kwargs.append(kwargs)
```

Then assert:

```python
assert sent_kwargs[0].get("parse_mode") is None
```

- [ ] **Step 2: Verify**

Run:

```powershell
uv run pytest --tb=short -q `
  tests/test_calibration_dryrun_scheduler.py::test_calibration_dryrun_scheduler_happy_path_fires_alert `
  tests/test_calibration_dryrun_scheduler.py::test_calibration_dryrun_scheduler_idempotency `
  tests/test_calibration_dryrun_scheduler.py::test_calibration_dryrun_passes_parse_mode_none_to_telegram
```

Expected: all pass.

---

## Task 4: Fix CoinGecko mcap-null heartbeat HTTP mocks

**Files:**
- Modify: `tests/test_heartbeat_mcap_missing.py`

- [ ] **Step 1: Reuse regex URL matching**

Add near imports or inside the test file:

```python
import re

MARKETS_PATTERN = re.compile(r"https://api\.coingecko\.com/api/v3/coins/markets")
```

Change both failing mocks from the bare URL string to:

```python
m.get(MARKETS_PATTERN, payload=payload, status=200, repeat=True)
```

This mirrors `tests/test_coingecko.py`, which already survives query params.

- [ ] **Step 2: Verify**

Run:

```powershell
uv run pytest --tb=short -q tests/test_heartbeat_mcap_missing.py
```

Expected: pass; no real `Connection refused` CoinGecko requests in output.

---

## Task 5: Preserve narrowed exception policy in narrative resolution test

**Files:**
- Modify: `tests/test_narrative_prediction_token_id.py`

- [ ] **Step 1: Raise the documented DB-resolution exception**

Change the monkeypatch in `test_resolution_check_error_fails_closed`:

```python
from scout.db import CoinIdResolutionError

async def _broken_resolves(coin_id):
    raise CoinIdResolutionError("simulated DB outage")
```

Do not change production `trade_predictions` to catch generic `RuntimeError`; current comments explicitly require only documented DB resolution errors to fail closed.

- [ ] **Step 2: Verify**

Run:

```powershell
uv run pytest --tb=short -q tests/test_narrative_prediction_token_id.py::test_resolution_check_error_fails_closed
```

Expected: pass.

---

## Task 6: Fix parse-mode hygiene at production call sites

**Files:**
- Modify: `scout/main.py`
- Test: `tests/test_parse_mode_hygiene.py`

- [ ] **Step 1: Add `parse_mode=None` to four `scout/main.py` dispatches**

Add the kwarg at the call sites currently reported by the AST hygiene test:

```python
await alerter.send_telegram_message(
    "...",
    session,
    settings,
    parse_mode=None,
)
```

```python
await send_telegram_message(chunk, session, settings, parse_mode=None)
```

```python
await send_telegram_message(msg, session, settings, parse_mode=None)
```

```python
await send_telegram_message(summary_text, session, settings, parse_mode=None)
```

Rationale: these bodies include system strings, LLM text, token tickers, or signal-derived summary content. Plain text is safer than relying on Telegram Markdown parsing.

- [ ] **Step 2: Verify**

Run:

```powershell
uv run pytest --tb=short -q tests/test_parse_mode_hygiene.py::test_all_dispatch_sites_pin_parse_mode
```

Expected: pass without needing to update `_ALLOWLIST_DISPATCH_SITES_WITHOUT_PARSE_MODE`.

---

## Task 7: Fix revival audit assertions for joint tg eligibility audit row

**Files:**
- Modify: `tests/test_signal_params_auto_suspend.py`

- [ ] **Step 1: Filter enabled audit queries**

In both failing tests, change audit SELECTs to include:

```sql
AND field_name='enabled'
```

For example:

```python
cur = await db._conn.execute(
    "SELECT field_name, old_value, new_value, applied_by, reason "
    "FROM signal_params_audit WHERE signal_type='gainers_early' "
    "AND field_name='enabled' "
    "ORDER BY applied_at DESC LIMIT 1"
)
```

- [ ] **Step 2: Add tg eligibility audit assertion**

In `test_revive_signal_with_baseline_stamps_baseline_and_audit`, assert the joint row exists:

```python
cur = await db._conn.execute(
    "SELECT old_value, new_value, applied_by FROM signal_params_audit "
    "WHERE signal_type='gainers_early' "
    "AND field_name='tg_alert_eligible' "
    "ORDER BY applied_at DESC LIMIT 1"
)
tg_old, tg_new, tg_by = await cur.fetchone()
assert tg_old == "0"
assert tg_new == "1"
assert tg_by == "operator"
```

- [ ] **Step 3: Verify**

Run:

```powershell
uv run pytest --tb=short -q `
  tests/test_signal_params_auto_suspend.py::test_revive_signal_with_baseline_stamps_baseline_and_audit `
  tests/test_signal_params_auto_suspend.py::test_revive_signal_with_baseline_on_already_enabled_signal
```

Expected: both pass.

---

## Task 8: Run clustered and full verification

**Files:**
- Modify: `tasks/todo.md` review section after execution

- [ ] **Step 1: Run the original red subset**

Run the reproduction command from the top of this plan.

Expected: all 17 pass.

- [ ] **Step 2: Run adjacent suites**

Run:

```powershell
uv run pytest --tb=short -q `
  tests/test_bl064_channel_reload.py `
  tests/test_bl076_junk_filter_and_symbol_name.py `
  tests/test_calibration_dryrun_scheduler.py `
  tests/test_heartbeat_mcap_missing.py `
  tests/test_narrative_prediction_token_id.py `
  tests/test_parse_mode_hygiene.py `
  tests/test_signal_params_auto_suspend.py
```

Expected: all pass.

- [ ] **Step 3: Run full suite with redirected output**

Run:

```powershell
uv run pytest --tb=short -q *> .pytest_baseline_fix_out.txt; $code=$LASTEXITCODE; Write-Output "EXIT=$code"; Get-Content .pytest_baseline_fix_out.txt -Tail 120
```

Expected: `EXIT=0` or a smaller remaining failure set with every remaining failure attributed in a review section.

- [ ] **Step 4: Cleanliness checks**

Run:

```powershell
git diff --check
git ls-files --eol tests/conftest.py tests/test_bl076_junk_filter_and_symbol_name.py tests/test_calibration_dryrun_scheduler.py tests/test_heartbeat_mcap_missing.py tests/test_narrative_prediction_token_id.py tests/test_signal_params_auto_suspend.py scout/main.py
```

Expected: no whitespace errors; touched files remain `i/lf w/lf`.

---

## Review Notes To Preserve

- Do not catch generic `RuntimeError` in `trade_predictions`; use `CoinIdResolutionError` in the test to preserve the narrowed-exception contract.
- Do not update parse-mode allowlist line numbers as the primary fix. Pin `parse_mode=None` at production call sites so future line drift does not matter.
- Do not reintroduce bare `Settings()` in unit tests. Use `settings_factory()` unless the test explicitly validates `.env` loading behavior.
- If a test creates a `Database`, ensure cleanup cannot be skipped by constructing Settings before `await db.initialize()` or by using `try/finally`.
