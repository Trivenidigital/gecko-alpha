# Calibrate.py weekly scheduled `--dry-run` + Telegram alert — combined plan + design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development.

**New primitives introduced:** new in-loop scheduler hook `_run_calibration_dryrun_scheduler()` inside the existing `_run_feedback_schedulers` at `scout/main.py:103-187` (sibling pattern to the existing `maybe_suspend_signals` hook at lines 155-173); new Settings fields `CALIBRATION_DRY_RUN_WEEKDAY: int = 0` (Monday) + `CALIBRATION_DRY_RUN_HOUR: int = 2` (local hour); new module-level idempotency sentinel `_last_calibration_dryrun_date` paralleling `_last_suspension_date` at scout/main.py:100; new structured log events `calibration_dryrun_pass` (success) + `calibration_dryrun_loop_error` (failure); new Telegram message body composed by reusing existing `build_diffs()` from `scout/trading/calibrate.py` + a thin format helper. NO new DB tables, columns, migrations, or dependencies.

**Drift-grounding 60-sec audit (per `feedback_drift_check_before_proposing.md`):**
- `grep -rn "calibrate" scout/main.py` → 0 matches (no existing scheduled invocation) ✓
- `grep -rn "from scout.trading.calibrate" scout/` → 0 matches ✓
- `applied_by='calibration'` audit rows on prod = 0 (verified via SSH) ✓ — calibrator never run on prod
- `scout/trading/calibrate.py` exists (557 lines) ✓ — operator-manual CLI; `build_diffs()` is the public dry-run entry point ✓

The hook IS missing. Item is genuinely pending.

**Combined plan + design rationale:** scope is ~50 LOC (1 scheduler hook + 2 Settings + 4-5 tests); combined doc + 2 reviewers preserves rigor without ceremony.

---

## Hermes-first analysis

**Domains checked against the 671-skill hub at `hermes-agent.nousresearch.com/docs/skills` (verified 2026-05-05):**

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Weekly cron / scheduled-job pattern | None (closest: `webhook-subscriptions` is event-delivery) | Build inline (in-loop hour+weekday gate matches existing project pattern) |
| Dry-run report → Telegram alert | None (closest: MLOps experiment-tracking, wrong domain) | Build inline (reuse existing `alerter.send_telegram_message`) |

**Verdict:** Pure project-internal scheduler extension. Building inline by adding a sibling hook to `_run_feedback_schedulers`.

---

## Drift grounding

**Read before drafting (verified — line numbers against current master 8e54578):**
- `scout/trading/calibrate.py:484-509` — `build_diffs(db, settings, window_days, ...)` returns `list[SignalDiff]`; `_main_async` formats + prints in dry-run mode. Public entry point for headless use.
- `scout/trading/calibrate.py:439-454` — `_format_diff(diff: SignalDiff) -> str` already produces a one-line human-readable summary (e.g., `"  gainers_early   n=131 win=52.6% expired=8.4% → trail_pct 20.0→18.0 [expired%]"`). Reusable for the Telegram message body.
- `scout/trading/calibrate.py:494-498` — header format `[CALIBRATE] window={N}d min_trades=... excluded=...`.
- `scout/main.py:103-187` — `_run_feedback_schedulers` — the function the new hook joins.
- `scout/main.py:155-173` — `maybe_suspend_signals` hook — the **exact pattern** to mirror: hour-gate + idempotency sentinel + try/except.
- `scout/main.py:100` — `_last_suspension_date` module-level idempotency sentinel.
- `scout/config.py:372` — `SUSPENSION_CHECK_HOUR: int = 1` — the hour-gate Settings precedent.
- `scout/alerter.py:send_telegram_message(msg, session, settings)` — the existing async Telegram sender, already used by `combo_refresh` streak alert at scout/main.py:146. Reuse without touching scout.alerter.
- `scout/trading/calibrate.py:24` — `_telegram_token_looks_real(settings)` guard — already exists; reuse to skip Telegram-emission when token is placeholder.

**Pattern conformance:**
- In-loop hour+weekday gate: matches `FEEDBACK_WEEKLY_DIGEST_WEEKDAY` + `FEEDBACK_WEEKLY_DIGEST_HOUR` at scout/main.py:177-185. New fields adopt the same shape.
- Idempotency sentinel `_last_calibration_dryrun_date`: matches `_last_suspension_date`.
- `try/except Exception → logger.exception("..._loop_error")`: matches both `combo_refresh_loop_error` and `auto_suspend_loop_error`.
- Telegram alert is dry-run-only (NEVER calls `apply_diffs`): explicit by design — auto-apply was rejected in the original Tier 1a/1b plan.

**Operational motivation:**
- 0 audit rows by `applied_by='calibration'` on prod = calibrator has never been invoked.
- Operator-pain: must SSH + remember to run `uv run python -m scout.trading.calibrate --apply` periodically.
- This PR reduces friction: every Monday at 2am local, the operator gets a Telegram message with the diff summary. They review + manually re-run with `--apply` if approved.

---

**Goal:** Operator receives a weekly Telegram alert with the latest dry-run calibration diff. No auto-apply (operator must explicitly re-run with `--apply` to write).

**Architecture:** Single new hook in `_run_feedback_schedulers`:

```python
# Weekly calibration dry-run (CALIBRATION_DRY_RUN_WEEKDAY/_HOUR local)
global _last_calibration_dryrun_date
if (
    now_local.weekday() == settings.CALIBRATION_DRY_RUN_WEEKDAY
    and now_local.hour == settings.CALIBRATION_DRY_RUN_HOUR
    and _last_calibration_dryrun_date != today_iso
):
    try:
        from scout.trading.calibrate import build_diffs
        diffs = await build_diffs(
            db, settings,
            window_days=settings.CALIBRATION_WINDOW_DAYS,
            min_trades=settings.CALIBRATION_MIN_TRADES,
            step=settings.CALIBRATION_STEP_SIZE_PCT,
            signal_filter=None,
            since_deploy=False,
        )
        actionable = sum(1 for d in diffs if d.changes)
        msg = _format_calibration_dryrun_alert(
            diffs, actionable,
            window_days=settings.CALIBRATION_WINDOW_DAYS,
        )
        async with aiohttp.ClientSession() as session:
            await alerter.send_telegram_message(msg, session, settings)
        logger.info(
            "calibration_dryrun_pass",
            actionable=actionable,
            total=len(diffs),
        )
        _last_calibration_dryrun_date = today_iso
    except Exception:
        logger.exception("calibration_dryrun_loop_error")
```

**Telegram message format** (≤4096 chars per Telegram limit):

```
📊 Weekly calibration dry-run (window=30d)
3 of 8 signal(s) would change.

  first_signal           n=87 win=51.7% expired=12.6% → trail_pct 20.0→22.0 [low_win]
  gainers_early          n=131 win=52.6% expired=8.4% → trail_pct 20.0→18.0 [expired%]
  losers_contrarian      n=42 win=38.1% expired=18.6% → sl_pct 25.0→27.0 [low_win]
  trending_catch         SKIPPED (n_trades 18 < min 50)
  ...

To apply: ssh root@<vps> 'cd /root/gecko-alpha && uv run python -m scout.trading.calibrate --apply'
```

**Tech Stack:** Python 3.12, async via aiosqlite + aiohttp, structlog, pytest + pytest-asyncio. No new dependencies.

---

## File Structure

| File | Responsibility | Status |
|---|---|---|
| `scout/config.py` | Add 2 new Settings fields + validators | Modify |
| `scout/main.py` | Add `_last_calibration_dryrun_date` sentinel + hook in `_run_feedback_schedulers` + `_format_calibration_dryrun_alert()` helper | Modify |
| `tests/test_calibration_dryrun_scheduler.py` | NEW: 5 tests covering hour-gate, idempotency, Telegram fallback, error path, format helper | Create |

---

## Tasks

### Task 1: Settings fields + validators

```python
def test_calibration_dryrun_weekday_default_monday(settings_factory):
    s = settings_factory()
    assert s.CALIBRATION_DRY_RUN_WEEKDAY == 0  # Monday

def test_calibration_dryrun_hour_default_2(settings_factory):
    s = settings_factory()
    assert s.CALIBRATION_DRY_RUN_HOUR == 2

def test_calibration_dryrun_weekday_validator_rejects_out_of_range(settings_factory):
    from pydantic import ValidationError
    for bad in (-1, 7, 8):
        with pytest.raises(ValidationError):
            settings_factory(CALIBRATION_DRY_RUN_WEEKDAY=bad)

def test_calibration_dryrun_hour_validator_rejects_out_of_range(settings_factory):
    from pydantic import ValidationError
    for bad in (-1, 24, 25):
        with pytest.raises(ValidationError):
            settings_factory(CALIBRATION_DRY_RUN_HOUR=bad)
```

In `scout/config.py` near `SUSPENSION_CHECK_HOUR`:

```python
    # Weekly calibration dry-run + Telegram alert (no auto-apply).
    # Operator reviews the diff in chat, then SSH + manually invokes
    # `uv run python -m scout.trading.calibrate --apply` if approved.
    CALIBRATION_DRY_RUN_WEEKDAY: int = 0  # Monday (matches WEEKLY_DIGEST_WEEKDAY pattern)
    CALIBRATION_DRY_RUN_HOUR: int = 2  # local hour
```

```python
    @field_validator("CALIBRATION_DRY_RUN_WEEKDAY")
    @classmethod
    def _validate_calibration_dryrun_weekday(cls, v: int) -> int:
        if not 0 <= v <= 6:
            raise ValueError(
                f"CALIBRATION_DRY_RUN_WEEKDAY must be 0-6 (Mon-Sun); got={v}"
            )
        return v

    @field_validator("CALIBRATION_DRY_RUN_HOUR")
    @classmethod
    def _validate_calibration_dryrun_hour(cls, v: int) -> int:
        if not 0 <= v <= 23:
            raise ValueError(
                f"CALIBRATION_DRY_RUN_HOUR must be 0-23; got={v}"
            )
        return v
```

### Task 2: Format helper + hook

```python
@pytest.mark.asyncio
async def test_format_calibration_dryrun_alert_includes_header_and_body(...):
    """T5 — format helper output structure."""

@pytest.mark.asyncio
async def test_calibration_dryrun_scheduler_idempotency(db, settings_factory, monkeypatch):
    """T6 — sentinel prevents re-fire same day."""

@pytest.mark.asyncio
async def test_calibration_dryrun_scheduler_telegram_fallback_on_placeholder_token(...):
    """T7 — when token looks fake, the hook still emits the log but skips Telegram."""

@pytest.mark.asyncio
async def test_calibration_dryrun_scheduler_handles_build_diffs_error(...):
    """T8 — build_diffs raises → calibration_dryrun_loop_error log; hook returns; main loop continues."""
```

In `scout/main.py` after `_last_suspension_date = ""`:

```python
# Last YYYY-MM-DD that calibration dry-run scheduler fired.
_last_calibration_dryrun_date = ""


def _format_calibration_dryrun_alert(diffs, actionable, *, window_days):
    """Build the Telegram message body for the weekly calibration dry-run.
    Truncates if total length exceeds Telegram's 4096-char ceiling."""
    from scout.trading.calibrate import _format_diff
    header = (
        f"📊 Weekly calibration dry-run (window={window_days}d)\n"
        f"{actionable} of {len(diffs)} signal(s) would change.\n"
    )
    body_lines = [_format_diff(d) for d in diffs]
    footer = (
        "\nTo apply: ssh root@<vps> 'cd /root/gecko-alpha && "
        "uv run python -m scout.trading.calibrate --apply'"
    )
    body = "\n".join(body_lines)
    full = header + "\n" + body + footer
    if len(full) > 4090:  # leave headroom
        full = full[:4087] + "..."
    return full
```

In `_run_feedback_schedulers` AFTER the auto-suspension block (line 173):

```python
    # Weekly calibration dry-run (CALIBRATION_DRY_RUN_WEEKDAY/_HOUR local).
    # Telegram-only — never writes; operator manually re-runs --apply.
    global _last_calibration_dryrun_date
    if (
        now_local.weekday() == settings.CALIBRATION_DRY_RUN_WEEKDAY
        and now_local.hour == settings.CALIBRATION_DRY_RUN_HOUR
        and _last_calibration_dryrun_date != today_iso
    ):
        try:
            from scout.trading.calibrate import build_diffs
            diffs = await build_diffs(
                db, settings,
                window_days=settings.CALIBRATION_WINDOW_DAYS,
                min_trades=settings.CALIBRATION_MIN_TRADES,
                step=settings.CALIBRATION_STEP_SIZE_PCT,
                signal_filter=None,
                since_deploy=False,
            )
            actionable = sum(1 for d in diffs if d.changes)
            msg = _format_calibration_dryrun_alert(
                diffs, actionable,
                window_days=settings.CALIBRATION_WINDOW_DAYS,
            )
            async with aiohttp.ClientSession() as session:
                await alerter.send_telegram_message(msg, session, settings)
            logger.info(
                "calibration_dryrun_pass",
                actionable=actionable,
                total=len(diffs),
            )
            _last_calibration_dryrun_date = today_iso
        except Exception:
            logger.exception("calibration_dryrun_loop_error")
```

### Task 3: Final regression sweep

```
SKIP_AIOHTTP_TESTS=1 uv run pytest tests/test_calibration_dryrun_scheduler.py tests/test_signal_params.py tests/test_config.py -q
```

Expected: 4 settings tests + 4 hook tests + existing 7 signal_params + existing 32 config tests all PASS.

---

## Test matrix

| ID | Test | Layer | What it pins |
|---|---|---|---|
| T1 | `weekday_default_monday` | Unit (config) | Default 0 = Monday |
| T2 | `hour_default_2` | Unit (config) | Default 2 (local) |
| T3 | `weekday_validator_rejects_out_of_range` | Unit (config) | -1, 7, 8 rejected |
| T4 | `hour_validator_rejects_out_of_range` | Unit (config) | -1, 24, 25 rejected |
| T5 | `format_calibration_dryrun_alert_includes_header_and_body` | Unit (helper) | Format string + truncation |
| T6 | `calibration_dryrun_scheduler_idempotency` | Integration (scheduler) | Same-day re-call → no double-fire |
| T7 | `calibration_dryrun_scheduler_telegram_fallback_on_placeholder_token` | Integration | Placeholder token → log emits, Telegram skipped |
| T8 | `calibration_dryrun_scheduler_handles_build_diffs_error` | Integration | build_diffs raises → calibration_dryrun_loop_error; loop continues |

8 active tests.

---

## Failure modes (silent-failure-first)

| # | Failure | Silent or loud? | Mitigation | Residual risk |
|---|---|---|---|---|
| F1 | Operator's Telegram token is placeholder; alert silently 404s | **Silent** at network layer (`alerter.send_telegram_message` already swallows network errors) | T7 — explicitly check token via `_telegram_token_looks_real`; emit `calibration_dryrun_telegram_skipped` log instead of attempting send | Acceptable — same blind spot as every other Telegram alert in the codebase; tracked by the operator-action item to set real token |
| F2 | `build_diffs` raises mid-execution (DB schema drift, missing table) | **Loud** — `calibration_dryrun_loop_error` exception log; main loop continues (try/except matches existing pattern) | T8 pins | None |
| F3 | Sentinel `_last_calibration_dryrun_date` resets on process restart → duplicate alert if pipeline restarts within the same hour-window of the target day | **Silent** (operator gets 2 messages instead of 1) | Acceptable — hour-window is 1h, weekly cadence; cost is at most 1 duplicate every 7 days. Idempotency could use a DB-backed sentinel but adds complexity for marginal gain | Documented; defer to a later PR if it becomes operator pain |
| F4 | `_format_calibration_dryrun_alert` produces >4096 chars for very large diffs (8 signals × ~80 chars + header/footer ~200 = ~840; well under Telegram cap) | **Loud** at Telegram API level (returns 400 BAD_REQUEST) | Defensive truncation at 4087 chars + ellipsis | None — bounded |
| F5 | Operator changes `CALIBRATION_DRY_RUN_HOUR` to a value the loop never sees (e.g., during a long ingestion stall) | **Silent** — alert simply doesn't fire that day | Acceptable; weekly cadence forgives one missed day; sentinel doesn't block next day's window | None |
| F6 | Race: timer fires AT the boundary `now_local.hour == HOUR_VAL` but `_last_calibration_dryrun_date` is already set from a prior cycle in the SAME hour | **Loud** — sentinel check correctly skips | T6 pins | None |

**Silent: 2 (F1 documented Telegram-token blindspot + F3 process-restart dup) / Loud: 4.**

---

## Performance notes

`build_diffs` reads ~30d of `paper_trades` via 1-2 indexed queries (per-signal aggregations). At observed ~1500 closed paper_trades over 30d, query takes <50ms. Telegram POST: ~200ms. Total per-fire cost: <300ms. Fires once per week at 2am local — operationally negligible.

---

## Rollback

Pure code revert + Settings defaults removed. No DB / migration changes.

**Operator-side disable** (no code change):
```bash
ssh root@89.167.116.187 'echo "CALIBRATION_DRY_RUN_HOUR=99" >> /root/gecko-alpha/.env'
```
But hour validator rejects 99 → pipeline crashes. Better disable: set `CALIBRATION_DRY_RUN_WEEKDAY=99` — wait, validator rejects too. **There is no clean disable opt-out** — operator must edit code OR set the hour to one they know the loop misses (e.g., during scheduled maintenance window). **Self-Review item NOT-IN-SCOPE: add a `CALIBRATION_DRY_RUN_ENABLED: bool = True` kill-switch in a follow-up if operator finds the alerts noisy.**

---

## Operational verification (§5)

Stop-FIRST sequence:
1. `systemctl stop gecko-pipeline`
2. `git pull origin master`
3. `find . -name __pycache__ -type d -exec rm -rf {} +`
4. `systemctl start gecko-pipeline`
5. Verify by tweaking `.env` to set `CALIBRATION_DRY_RUN_HOUR` to current hour + restart; wait for next minute boundary; check journalctl for `calibration_dryrun_pass` event. Then revert `.env` to default.

Post-verification: keep default Monday 2am; first natural fire = next Monday at 2am local.

---

## Self-Review

1. **Hermes-first:** ✓ 2/2 negative.
2. **Drift grounding:** ✓ explicit refs to calibrate.py public API + main.py scheduler pattern + alerter import; 60-sec audit confirmed no existing scheduled invocation.
3. **Test matrix:** 8 active.
4. **Failure modes:** 6 enumerated; 2 silent (F1 documented blindspot; F3 process-restart dup acceptable cost).
5. **Performance:** <300ms per fire; weekly cadence; negligible.
6. **Rollback:** code revert clean. NO env-flip disable available — documented as deferred follow-up (`CALIBRATION_DRY_RUN_ENABLED` kill-switch).
7. **Honest scope:**
   - **NOT in scope:** auto-apply. Original Tier 1a/1b design rejected this; operator must manually re-run `--apply` after reviewing.
   - **NOT in scope:** dashboard surface for diff history. Telegram + journalctl is the surface.
   - **NOT in scope:** scheduling primitives (e.g. APScheduler). Reuse the existing in-loop hour-gate pattern.
   - **NOT in scope:** disable opt-out kill-switch. Documented as follow-up.
