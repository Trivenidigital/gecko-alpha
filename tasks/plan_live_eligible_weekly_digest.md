**New primitives introduced:** `scout/trading/cohort_digest.py` module (`build_cohort_digest()` + `send_cohort_digest()` + `_compute_signal_cohort_stats()` + `_classify_verdict()` + `_detect_verdict_flip()`); Settings fields `COHORT_DIGEST_ENABLED: bool = True`, `COHORT_DIGEST_N_GATE: int = 10`, `COHORT_DIGEST_DAY_OF_WEEK: int = 0` (Monday, mirroring `WEEKLY_DIGEST_DAY_OF_WEEK` default); structured log events `cohort_digest_sent` (info) / `cohort_digest_empty` (info) / `cohort_digest_failed` (exception) / `cohort_digest_verdict_flip` (warning per-signal-flip); module-level state file `last_cohort_digest_date` tracked in `main.py` weekly-loop (mirrors `last_weekly_digest_date`); pre-registered final-window summary alert on 2026-06-08; `BL-NEW-COHORT-DIGEST-DECISION` evidence-gated follow-up filed at ship time.

# Plan: BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST

**Backlog item:** `BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST` (filed 2026-05-12 Vector C review of dashboard cohort PR)
**Goal:** weekly Telegram alert summarizing the would_be_live=1 cohort vs paper cohort per signal_type, with verdict classification + sign-flip detection. Final 2026-06-08 alert carries the 4-week decision recommendation.

**Architecture:**

- New module `scout/trading/cohort_digest.py` parallels `scout/trading/weekly_digest.py`. Shape: `build_cohort_digest(db, end_date, settings) -> str | None` + `send_cohort_digest(db, settings) -> None`.
- Stateless: weekly comparison reads two windows from `paper_trades` (this week + last week), no new persistence table needed (compose with existing `would_be_live` column).
- Sign-flip detection: derive verdict for week N and week N-1 from the same DB at run time; emit `cohort_digest_verdict_flip` WARNING per signal_type whose label changed.
- 4-week final-window verdict alert: on the run nearest 2026-06-08, include the locked decision-recommendation block (Promote / Track-Wider / Reject per the dashboard view's logic).
- Cron lives in `main.py` weekly-loop (same surface as `_weekly_digest.send_weekly_digest`) to share the existing daily-loop scaffold; gated by `last_cohort_digest_date`.

**Tech stack:** Python 3.12, aiosqlite, structlog, existing `scout.trading.weekly_digest` + `scout.analytics` patterns.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Scheduled cohort-comparison digests | None (hermes-agent.nousresearch.com/docs/skills probed 2026-05-17, 689 skills indexed) | Build in-tree. |
| Generic weekly digest dispatch | None (architectural neighbor `scout/trading/weekly_digest.py` already in-tree, mirror its shape) | Reuse pattern. |

awesome-hermes-agent: 404 (consistent). **Verdict:** custom; mirror `weekly_digest.py` shape.

## Drift verdict

NET-NEW confirmed 2026-05-17. `scout/trading/weekly_digest.py` exists but is signal-PnL/leaderboard focused — no would_be_live cohort comparison. `scout/trading/digest.py` is daily paper digest. `scout/narrative/digest.py` is narrative-alert formatting. None cover the cohort-comparison shape backlog §625-638 requires.

## File structure

| File | Responsibility |
|---|---|
| `scout/trading/cohort_digest.py` (NEW) | build + send cohort digest; pure logic + dispatch |
| `scout/config.py` (MODIFY) | 3 Settings fields (enabled / n_gate / day_of_week) |
| `scout/main.py` (MODIFY) | wire weekly-loop call + `last_cohort_digest_date` tracking, mirror weekly_digest hook |
| `tests/test_cohort_digest.py` (NEW) | unit tests for build / verdict classification / sign-flip / final-window block |
| `tests/test_cohort_digest_main_hook.py` (NEW) | integration test for main.py weekly-loop gating |
| `tasks/plan_live_eligible_weekly_digest.md` (THIS) | plan |
| `tasks/design_live_eligible_weekly_digest.md` (cycle 5 step 7) | design |

## Pre-registered decision criteria (BL-NEW-COHORT-DIGEST-DECISION)

| Observation over 4 weeks (until 2026-06-08) | Verdict | Action |
|---|---|---|
| Per-signal `cohort_digest_verdict_flip` events ≥ 2 within window (instability) | EXTEND eval window 4w | File `BL-NEW-COHORT-DIGEST-DECISION-EXTENDED` |
| 4 consecutive weekly cohort digests with **stable** verdict (zero `verdict_flip`) AND any signal classifies "Strong" (Δwr ≥ +10pp, n≥10) | PROMOTE that signal to live | File live-trading runbook entry |
| 4 consecutive weekly digests stable AND every signal classifies "Tracking" or "Weak" | TRACK-WIDER | File `BL-NEW-COHORT-DIGEST-EXTEND-4w` to extend |
| Operator missed the final 2026-06-08 alert (manual digest re-run option) | RE-RUN | `python -m scout.trading.cohort_digest --backfill` |

n-gate is `COHORT_DIGEST_N_GATE=10` (matches dashboard decision-locked 2026-06-08 verdict-floor per memory `project_dashboard_cohort_view_shipped_2026_05_12.md`).

## Verdict-classification rule (locked)

```
Δwr = would_be_live_win_rate_pct − paper_cohort_win_rate_pct

Strong:    Δwr ≥ +10pp  AND  n_would_be_live ≥ COHORT_DIGEST_N_GATE
Tracking:  -2pp ≤ Δwr < +10pp  AND  n_would_be_live ≥ COHORT_DIGEST_N_GATE
Weak:      Δwr < -2pp  AND  n_would_be_live ≥ COHORT_DIGEST_N_GATE
INSUFFICIENT_DATA:  n_would_be_live < COHORT_DIGEST_N_GATE
```

Mirrors dashboard cohort-view logic (per memory `feedback_n_gate_verdicts_against_dashboard_noise.md` — verdict surfaces MUST n-gate + show INSUFFICIENT_DATA explicitly).

## Sign-flip detection rule

For each signal_type with n_would_be_live ≥ n_gate in BOTH week N and week N-1, compare verdict labels. If they differ AND both are non-INSUFFICIENT_DATA, emit `cohort_digest_verdict_flip` WARNING + include FLIP marker line in the digest text.

Transitions to/from INSUFFICIENT_DATA are NOT flips — those reflect n-rate, not regime change.

## Telegram message shape (target ≤ 4KB to fit one TG chunk)

```
Cohort Digest — Week of YYYY-MM-DD → YYYY-MM-DD

Signal              | n_live | wr_live | n_paper | wr_paper | Δwr   | Verdict
gainers_early       |     14 |   71.4% |      27 |    66.7% |  +4.7 | Tracking
losers_contrarian   |     11 |   72.7% |      19 |    63.2% |  +9.5 | Tracking
first_signal        |     22 |   77.3% |      35 |    74.3% |  +3.0 | Tracking
trending_catch      |      8 |   62.5% |      14 |    57.1% |  +5.4 | INSUFFICIENT_DATA
slow_burn          |      6 |   66.7% |      11 |    54.5% | +12.2 | INSUFFICIENT_DATA

⚠ verdict_flip: first_signal Strong → Tracking (week-over-week)

Window: 4w toward 2026-06-08 decision point
[at 2026-06-08 final run, decision-recommendation block appended]
```

## Task decomposition (TDD, bite-sized)

### Task 1: Settings + tests

**Files:**
- Modify: `scout/config.py` (add 3 fields)
- Test: `tests/test_config.py` (3 default-value tests)

- [ ] **Step 1 — Write failing tests** in `tests/test_config.py`:

```python
def test_cohort_digest_enabled_default():
    s = Settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.COHORT_DIGEST_ENABLED is True

def test_cohort_digest_n_gate_default():
    s = Settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.COHORT_DIGEST_N_GATE == 10

def test_cohort_digest_day_of_week_default():
    s = Settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.COHORT_DIGEST_DAY_OF_WEEK == 0  # Monday
```

- [ ] **Step 2 — Run, expect fail** `uv run pytest tests/test_config.py::test_cohort_digest_enabled_default -v`
- [ ] **Step 3 — Implement** in `scout/config.py`:

```python
COHORT_DIGEST_ENABLED: bool = True
COHORT_DIGEST_N_GATE: int = 10
COHORT_DIGEST_DAY_OF_WEEK: int = 0  # Monday
```

- [ ] **Step 4 — Run, expect pass**
- [ ] **Step 5 — Commit** `feat(config): cohort digest settings (cycle 5 commit 1/4)`

### Task 2: Stats computation + verdict classification + tests

**Files:**
- Create: `scout/trading/cohort_digest.py` (initial — `_compute_signal_cohort_stats` + `_classify_verdict`)
- Test: `tests/test_cohort_digest.py`

- [ ] **Step 1 — Write failing tests** in `tests/test_cohort_digest.py`:

```python
import pytest
from scout.trading.cohort_digest import _classify_verdict, _compute_signal_cohort_stats

def test_classify_verdict_strong():
    assert _classify_verdict(delta_pp=10.5, n_live=15, n_gate=10) == "Strong"

def test_classify_verdict_tracking():
    assert _classify_verdict(delta_pp=4.7, n_live=14, n_gate=10) == "Tracking"

def test_classify_verdict_weak():
    assert _classify_verdict(delta_pp=-5.0, n_live=12, n_gate=10) == "Weak"

def test_classify_verdict_insufficient_data():
    assert _classify_verdict(delta_pp=99.0, n_live=5, n_gate=10) == "INSUFFICIENT_DATA"

def test_classify_verdict_strong_boundary_exclusive_at_10pp():
    # +10pp boundary: Strong is ≥+10pp per locked rule
    assert _classify_verdict(delta_pp=10.0, n_live=10, n_gate=10) == "Strong"
    assert _classify_verdict(delta_pp=9.99, n_live=10, n_gate=10) == "Tracking"

async def test_compute_signal_cohort_stats_basic(db, token_factory):
    # seed paper_trades: 3 would_be_live wins, 1 would_be_live loss,
    # 2 paper-only wins, 1 paper-only loss for signal='gainers_early'
    ...
    stats = await _compute_signal_cohort_stats(
        db, signal_type="gainers_early", start=..., end=...
    )
    assert stats["n_live"] == 4
    assert stats["wr_live"] == 75.0
    assert stats["n_paper"] == 7
    assert stats["wr_paper"] == pytest.approx(71.43, abs=0.01)
    assert stats["delta_pp"] == pytest.approx(3.57, abs=0.01)
```

- [ ] **Step 2 — Run, expect fail**
- [ ] **Step 3 — Implement** in `scout/trading/cohort_digest.py`:

```python
"""Cohort digest builder — weekly would_be_live vs paper-cohort comparison."""

from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
import structlog
from scout.config import Settings
from scout.db import Database

log = structlog.get_logger()


def _classify_verdict(*, delta_pp: float, n_live: int, n_gate: int) -> str:
    if n_live < n_gate:
        return "INSUFFICIENT_DATA"
    if delta_pp >= 10.0:
        return "Strong"
    if delta_pp >= -2.0:
        return "Tracking"
    return "Weak"


async def _compute_signal_cohort_stats(
    db: Database, *, signal_type: str, start: datetime, end: datetime
) -> dict:
    """Returns dict with n_live, wr_live, n_paper, wr_paper, delta_pp for
    closed trades (status='closed') in the [start, end) window for the
    given signal_type. n_paper / wr_paper include all closed trades
    (paper cohort = full population); n_live / wr_live filter to
    would_be_live=1.
    """
    # ... aiosqlite SELECT COUNT(*) FILTER (WHERE ...) + SUM(pnl_pct > 0) ...
```

- [ ] **Step 4 — Run, expect pass**
- [ ] **Step 5 — Commit** `feat(cohort_digest): stats + verdict classification (cycle 5 commit 2/4)`

### Task 3: Digest text builder + sign-flip detection + tests

**Files:**
- Modify: `scout/trading/cohort_digest.py` (add `build_cohort_digest` + `_detect_verdict_flip`)
- Test: `tests/test_cohort_digest.py`

- [ ] **Step 1 — Write failing tests**:

```python
async def test_build_cohort_digest_emits_signal_rows(db, settings_factory):
    # seed week-N data for 2 signals
    text = await build_cohort_digest(db, end_date=date(2026,5,17), settings=settings_factory())
    assert "gainers_early" in text
    assert "Tracking" in text
    assert "n_live" in text or "| n_live |" in text

async def test_build_cohort_digest_returns_none_when_no_activity(db, settings_factory):
    text = await build_cohort_digest(db, end_date=date(2026,5,17), settings=settings_factory())
    assert text is None

async def test_detect_verdict_flip_emits_warning_when_label_changes():
    """Strong → Tracking week-over-week with both n above gate is a flip."""
    flips = _detect_verdict_flip(
        current={"gainers_early": "Strong"},
        previous={"gainers_early": "Tracking"},
    )
    assert flips == [("gainers_early", "Tracking", "Strong")]

async def test_detect_verdict_flip_ignores_insufficient_data_transitions():
    """INSUFFICIENT_DATA → Tracking is NOT a flip (n-rate change, not regime)."""
    flips = _detect_verdict_flip(
        current={"slow_burn": "Tracking"},
        previous={"slow_burn": "INSUFFICIENT_DATA"},
    )
    assert flips == []

async def test_build_cohort_digest_includes_final_window_block_on_2026_06_08(db, settings_factory):
    text = await build_cohort_digest(db, end_date=date(2026,6,8), settings=settings_factory())
    assert "4-week decision point" in text
    assert "Promote" in text or "Track-Wider" in text or "Reject" in text
```

- [ ] **Step 2 — Run, expect fail**
- [ ] **Step 3 — Implement** in `scout/trading/cohort_digest.py`
- [ ] **Step 4 — Run, expect pass**
- [ ] **Step 5 — Commit** `feat(cohort_digest): text builder + sign-flip detection (cycle 5 commit 3/4)`

### Task 4: send_cohort_digest dispatch + main.py weekly-loop hook + tests

**Files:**
- Modify: `scout/trading/cohort_digest.py` (add `send_cohort_digest`)
- Modify: `scout/main.py` (wire weekly-loop call mirroring `_weekly_digest.send_weekly_digest`)
- Create: `tests/test_cohort_digest_main_hook.py`

- [ ] **Step 1 — Write failing tests**:

```python
# tests/test_cohort_digest_main_hook.py
async def test_main_weekly_loop_calls_send_cohort_digest_on_configured_day(...):
    """COHORT_DIGEST_DAY_OF_WEEK=0 (Monday): on a Monday with no prior run,
    send_cohort_digest fires once + last_cohort_digest_date updates."""
    ...

async def test_main_weekly_loop_skips_when_disabled(...):
    """COHORT_DIGEST_ENABLED=False: send_cohort_digest not called."""
    ...

async def test_main_weekly_loop_doesnt_re_fire_same_day(...):
    """last_cohort_digest_date already set to today: skip."""
    ...
```

- [ ] **Step 2 — Run, expect fail**
- [ ] **Step 3 — Implement** `send_cohort_digest` + main.py hook
- [ ] **Step 4 — Run, expect pass + full regression on VPS**
- [ ] **Step 5 — Commit** `feat(main): cohort digest weekly-loop hook (cycle 5 commit 4/4)` then `docs(backlog): close BL-NEW-LIVE-ELIGIBLE-WEEKLY-DIGEST + file BL-NEW-COHORT-DIGEST-DECISION`

## Decision criteria pre-registered

(see table above)

## Memory checkpoint to create at ship time

`~/.claude/projects/C--projects-gecko-alpha/memory/project_cohort_digest_decision_2026_06_08.md` with:
- Deploy commit + PR
- Trigger date 2026-06-08 (locked per dashboard cohort-view ship checkpoint)
- Pre-registered criteria copy
- Revert path (`COHORT_DIGEST_ENABLED=False` + restart)

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `would_be_live` column populated only post-cycle-3 cutover → week N-1 may have NULL rows | Medium | Medium | `_compute_signal_cohort_stats` filters `would_be_live IS NOT NULL` (per memory `feedback_mid_flight_flag_migration.md`); pre-cutover comparison reads as INSUFFICIENT_DATA cleanly |
| Sign-flip alert noise during early weeks when n approaches gate | Medium | Low | INSUFFICIENT_DATA transitions are NOT flips; only n≥gate↔n≥gate label changes emit |
| Telegram message > 4KB at 5+ signals | Low | Low | Truncate to top 5 by `n_live` desc + footer "(N more signals omitted)" |
| Final-window block date mis-match | Low | Medium | Pin via `end_date == COHORT_DIGEST_FINAL_DATE`; explicit Settings field for the trigger date so calendar shifts adjust cleanly |
| Pre-existing weekly_digest fires on same Monday → operator sees 2 messages | Medium | Low | Acceptable — different surfaces (PnL vs cohort); operator can disable cohort via flag |

## Out of scope

- Daily-cadence cohort digest — backlog explicitly says "weekly"
- Cross-signal aggregation — per-signal-only; aggregate is the dashboard's job
- Actual paper→live promotion automation — decision-recommendation block is operator-facing, not auto-act
- Backfill mode (`--backfill`) — mentioned in decision-criteria table for operator re-run, deferred to follow-up if 4-week run requires it

## Deployment verification (autonomous post-3-reviewer-fold)

1. `find . -name __pycache__ -exec rm -rf {} +` on srilu after pull
2. `systemctl restart gecko-pipeline` + `systemctl is-active`
3. Smoke: `journalctl -u gecko-pipeline --since "5 minutes ago" | grep -E "cohort_digest|weekly_digest"` shows main-loop registered next-Monday firing
4. Memory checkpoint already filed pre-merge
5. First firing: next Monday after merge (per `COHORT_DIGEST_DAY_OF_WEEK=0`); operator confirms TG receipt
6. 2026-06-08 final-window: confirm decision-recommendation block present
