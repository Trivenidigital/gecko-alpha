**New primitives introduced:** `scout/trading/cohort_digest.py` module (`build_cohort_digest()` + `send_cohort_digest()` + `_compute_signal_cohort_stats()` + `_classify_verdict()` + `_detect_verdict_flip()`); Settings fields `COHORT_DIGEST_ENABLED: bool = True`, `COHORT_DIGEST_N_GATE: int = 10`, `COHORT_DIGEST_DAY_OF_WEEK: int = 0` (Monday, mirroring `WEEKLY_DIGEST_DAY_OF_WEEK` default), `COHORT_DIGEST_FINAL_DATE: date = 2026-06-08`, `COHORT_DIGEST_STRONG_WR_GAP_PP: float = 15.0`, `COHORT_DIGEST_STRONG_PNL_FLOOR_USD: float = 200.0`, `COHORT_DIGEST_MODERATE_WR_GAP_PP: float = 5.0`; module-level `_NEAR_IDENTICAL_COHORTS = ("chain_completed",)` + `_LIVE_ELIGIBLE_ENUMERATED_TYPES = ("chain_completed", "volume_spike", "gainers_early")` (both mirroring `dashboard/db.py`); structured log events `cohort_digest_sent` (info) / `cohort_digest_empty` (info) / `cohort_digest_failed` (exception) / `cohort_digest_verdict_flip` (warning, ONE rolled-up event per digest containing all signal flips); module-level state `last_cohort_digest_date` + `last_final_block_fired` tracked in `main.py` weekly-loop (mirrors `last_weekly_digest_date`); pre-registered final-window summary alert on first run with `end_date >= COHORT_DIGEST_FINAL_DATE`; `BL-NEW-COHORT-DIGEST-DECISION` evidence-gated follow-up filed at ship time.

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
| Per-signal flip events ≥ 2 within window (instability) | EXTEND eval window 4w | File `BL-NEW-COHORT-DIGEST-DECISION-EXTENDED` |
| 4 consecutive weekly digests **stable** AND any enumerated signal classifies "strong-pattern (exploratory)" | RECOMMEND-LIVE-REVIEW (exploratory) | Per V28 SHOULD-FIX: NOT auto-promote. Operator review for live-promotion candidacy; gating dependency is BL-055 live-trading unlock (still deferred). File operator-decision artifact + memory checkpoint. |
| 4 consecutive weekly digests stable AND every enumerated signal classifies "moderate" or "tracking" | TRACK-WIDER | File `BL-NEW-COHORT-DIGEST-EXTEND-4w` to extend |
| All enumerated signals stuck at INSUFFICIENT_DATA at 2026-06-08 | INCONCLUSIVE | File `BL-NEW-COHORT-DIGEST-INCONCLUSIVE` — eval-window-too-short evidence; consider widening writer-deployment back-fill |

n-gate is `COHORT_DIGEST_N_GATE=10` (matches dashboard's `min_eligible_n_for_verdict` per memory `project_dashboard_cohort_view_shipped_2026_05_12.md` + `dashboard/db.py:1263`). All four thresholds (`STRONG_WR_GAP_PP / STRONG_PNL_FLOOR_USD / MODERATE_WR_GAP_PP / N_GATE`) are also kept in lockstep with the dashboard so a single retune (`.env` override + restart) reconciles both surfaces.

## Verdict-classification rule (locked — verbatim mirror of dashboard)

**V28 MUST-FIX fold (2026-05-17):** the cycle-5 plan originally used asymmetric `≥+10pp / -2pp / Weak` labels — that did NOT match `dashboard/frontend/components/TradingTab.jsx:389-425` + `dashboard/db.py:1263`. Operator would have seen two surfaces giving different verdicts on the same signal at 2026-06-08. Plan is rewritten to mirror the dashboard verbatim:

```python
# Inputs (per signal_type, within the [end-7d, end) window):
#   fN, fWins, fPnl  = full_cohort   (status != 'open', closed_at in window)
#   eN, eWins, ePnl  = eligible_cohort (same + would_be_live=1)
#
# Derived:
fWr      = (fWins / fN * 100) if fN > 0 else 0
eWr      = (eWins / eN * 100) if eN > 0 else None
wrDelta  = (eWr - fWr) if eWr is not None else None
signFlipRaw = ((fPnl > 0) and (ePnl < 0)) or ((fPnl < 0) and (ePnl > 0))

strongPattern = (
    signFlipRaw
    and abs(fPnl) >= COHORT_DIGEST_STRONG_PNL_FLOOR_USD   # 200
    and abs(ePnl) >= COHORT_DIGEST_STRONG_PNL_FLOOR_USD   # 200
    and wrDelta is not None
    and abs(wrDelta) > COHORT_DIGEST_STRONG_WR_GAP_PP     # 15 (STRICT >, NOT >=)
)

if signal_type in _NEAR_IDENTICAL_COHORTS:
    verdict = "near-identical"
elif eN == 0:
    verdict = "INSUFFICIENT_DATA (n=0)"
elif eN < n_gate:                                          # n_gate = 10
    verdict = f"INSUFFICIENT_DATA (n={eN}, need >={n_gate})"
elif strongPattern:
    verdict = "strong-pattern (exploratory)"
elif signFlipRaw or (wrDelta is not None and abs(wrDelta) > COHORT_DIGEST_MODERATE_WR_GAP_PP):
    verdict = "moderate"
else:
    verdict = "tracking"
```

Constants pulled into Settings so a single edit (`.env` override + restart) reconciles dashboard + digest in lockstep if the threshold is ever retuned. Per memory `feedback_n_gate_verdicts_against_dashboard_noise.md` — the digest will be operator-compared against the dashboard, so the verdict-label-and-threshold are load-bearing.

**Structural exclusion** (V28 MUST-FIX): signal_types in `_LIVE_ELIGIBLE_ENUMERATED_TYPES = ("chain_completed", "volume_spike", "gainers_early")` are the ONLY signals that meaningfully stack to ≥3 (Tier 1a+) — every other signal has structurally-empty eligible cohort. Mirror `dashboard/db.py:1097` exactly. Digest will only verdict the 3 enumerated types; other signal_types appear in an "excluded" footer with structural-reason text matching dashboard's `_to_row` excluded payload.

## Sign-flip detection rule

For each signal_type in `_LIVE_ELIGIBLE_ENUMERATED_TYPES` with eN ≥ n_gate in BOTH week N and week N-1, compare verdict labels (full set: `near-identical / strong-pattern (exploratory) / moderate / tracking`). If labels differ AND both are non-`INSUFFICIENT_DATA*`, record a flip.

**V28 SHOULD-FIX fold:** emit ONE rolled-up `cohort_digest_verdict_flip` WARNING per digest containing all flips, NOT one per signal. Digest text gets a SINGLE summary line:

```
⚠ FLIPS THIS WEEK: gainers_early (S→T), first_signal (T→S)
```

Transitions to/from `INSUFFICIENT_DATA*` are NOT flips (n-rate not regime).

## Telegram message shape (target ≤ 4KB to fit one TG chunk)

**V28 NICE-TO-HAVE fold:** vertical one-block-per-signal layout for mobile readability (most TG reads are mobile). Tables wrap poorly on phone.

```
Cohort Digest — Week of YYYY-MM-DD → YYYY-MM-DD
n-gate: eN ≥ 10 for a verdict.

[gainers_early]
  eligible: n=14, wr=71.4%, pnl=$+842
  full:     n=27, wr=66.7%, pnl=$+1,205
  Δwr=+4.7pp, signFlip=no → tracking

[volume_spike]
  eligible: n=12, wr=58.3%, pnl=$-318
  full:     n=22, wr=63.6%, pnl=$+450
  Δwr=-5.3pp, signFlip=YES → moderate (PnL floor not met → not strong-pattern)

[chain_completed]
  near-identical (Tier 1a — eligible ≈ full by construction; verdict not informative)

Excluded (structural — single-source, max stack < 3):
  losers_contrarian, first_signal, trending_catch, slow_burn, ...

⚠ FLIPS THIS WEEK: gainers_early (tracking → moderate)

Window: 4w toward 2026-06-08 decision point
[at 2026-06-08+ first run, decision-recommendation block appended — see below]
```

Decision-recommendation block at final run (V28 SHOULD-FIX softening — no live-trading runbook yet, BL-055 still gated):

```
=== 4-week decision point (2026-06-08 anchor) ===
Strong-pattern signals (exploratory): [list]
  → Recommend operator review for live-promotion candidacy.
    (BL-055 live-trading unlock is the gating dependency; this is a
    pre-approval signal, not an auto-promote.)
Moderate signals: [list]
  → Continue paper soak; re-evaluate at +4w.
Tracking signals: [list]
  → No regime change observed. Continue as-is.
Near-identical / Excluded: [list]
  → Structural — verdict not informative.
```

## Task decomposition (TDD, bite-sized)

### Task 1: Settings + tests

**Files:**
- Modify: `scout/config.py` (add 3 fields)
- Test: `tests/test_config.py` (3 default-value tests)

- [ ] **Step 1 — Write failing tests** in `tests/test_config.py`:

```python
from datetime import date

def test_cohort_digest_enabled_default():
    s = Settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.COHORT_DIGEST_ENABLED is True

def test_cohort_digest_n_gate_default():
    s = Settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.COHORT_DIGEST_N_GATE == 10

def test_cohort_digest_day_of_week_default():
    s = Settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.COHORT_DIGEST_DAY_OF_WEEK == 0  # Monday

def test_cohort_digest_final_date_default():
    s = Settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.COHORT_DIGEST_FINAL_DATE == date(2026, 6, 8)

def test_cohort_digest_threshold_defaults_mirror_dashboard():
    """V28 fold: thresholds match dashboard/frontend/components/TradingTab.jsx:161-162."""
    s = Settings(TELEGRAM_BOT_TOKEN="t", TELEGRAM_CHAT_ID="c", ANTHROPIC_API_KEY="k")
    assert s.COHORT_DIGEST_STRONG_WR_GAP_PP == 15.0
    assert s.COHORT_DIGEST_STRONG_PNL_FLOOR_USD == 200.0
    assert s.COHORT_DIGEST_MODERATE_WR_GAP_PP == 5.0
```

- [ ] **Step 2 — Run, expect fail** `uv run pytest tests/test_config.py -k cohort_digest -v`
- [ ] **Step 3 — Implement** in `scout/config.py`:

```python
from datetime import date

# ... inside Settings class ...
COHORT_DIGEST_ENABLED: bool = True
COHORT_DIGEST_N_GATE: int = 10
COHORT_DIGEST_DAY_OF_WEEK: int = 0  # Monday
COHORT_DIGEST_FINAL_DATE: date = date(2026, 6, 8)
COHORT_DIGEST_STRONG_WR_GAP_PP: float = 15.0     # dashboard parity
COHORT_DIGEST_STRONG_PNL_FLOOR_USD: float = 200.0  # dashboard parity
COHORT_DIGEST_MODERATE_WR_GAP_PP: float = 5.0    # dashboard parity
```

- [ ] **Step 4 — Run, expect pass**
- [ ] **Step 5 — Commit** `feat(config): cohort digest settings (cycle 5 commit 1/4)`

### Task 2: Stats computation + verdict classification + tests

**V27 MUST-FIX fold (2026-05-17):** stats query mirrors `dashboard/db.py:1148-1170` verbatim — `status != 'open'`, `closed_at` (NOT `opened_at`), `SUM(CASE WHEN pnl_usd > 0 ...)`. A literal `status='closed'` filter or `pnl_pct`-based win would silently under-count by ~80% (closed_tp/closed_sl/closed_moonshot/expired) and the digest would diverge from the dashboard.

**Files:**
- Create: `scout/trading/cohort_digest.py` (initial — `_compute_signal_cohort_stats` + `_classify_verdict`)
- Test: `tests/test_cohort_digest.py`

- [ ] **Step 1 — Write failing tests** in `tests/test_cohort_digest.py`:

```python
import pytest
from scout.trading.cohort_digest import _classify_verdict, _compute_signal_cohort_stats

# V27/V28 fold: tests now exercise the dashboard-mirroring rule exactly.

def test_classify_verdict_strong_pattern_requires_all_four_conditions():
    # signFlip + |fPnl|>=200 + |ePnl|>=200 + |wrDelta|>15 (STRICT)
    v = _classify_verdict(eN=15, fN=30, wrDelta=16.0, fPnl=300, ePnl=-250,
                          signal_type="gainers_early", n_gate=10)
    assert v == "strong-pattern (exploratory)"

def test_classify_verdict_strict_inequality_at_15pp_falls_to_moderate():
    # exactly 15pp does NOT qualify (STRICT >), falls to moderate via |wrDelta|>5
    v = _classify_verdict(eN=15, fN=30, wrDelta=15.0, fPnl=300, ePnl=-250,
                          signal_type="gainers_early", n_gate=10)
    assert v == "moderate"

def test_classify_verdict_strong_pnl_floor_not_met_falls_to_moderate():
    # signFlip + |wrDelta|>15 BUT |fPnl|<200 → moderate, not strong-pattern
    v = _classify_verdict(eN=15, fN=30, wrDelta=20.0, fPnl=150, ePnl=-250,
                          signal_type="gainers_early", n_gate=10)
    assert v == "moderate"

def test_classify_verdict_moderate_via_signflip_alone():
    v = _classify_verdict(eN=15, fN=30, wrDelta=2.0, fPnl=100, ePnl=-50,
                          signal_type="gainers_early", n_gate=10)
    assert v == "moderate"

def test_classify_verdict_moderate_via_wrgap_alone():
    # no signFlip + |wrDelta|=6 > 5 → moderate
    v = _classify_verdict(eN=15, fN=30, wrDelta=6.0, fPnl=300, ePnl=200,
                          signal_type="gainers_early", n_gate=10)
    assert v == "moderate"

def test_classify_verdict_tracking_below_moderate_threshold():
    v = _classify_verdict(eN=15, fN=30, wrDelta=2.0, fPnl=300, ePnl=200,
                          signal_type="gainers_early", n_gate=10)
    assert v == "tracking"

def test_classify_verdict_near_identical_for_chain_completed():
    # chain_completed always gets near-identical regardless of stats
    v = _classify_verdict(eN=50, fN=60, wrDelta=20.0, fPnl=500, ePnl=-400,
                          signal_type="chain_completed", n_gate=10)
    assert v == "near-identical"

def test_classify_verdict_insufficient_data_n_zero():
    v = _classify_verdict(eN=0, fN=10, wrDelta=None, fPnl=0, ePnl=0,
                          signal_type="gainers_early", n_gate=10)
    assert v == "INSUFFICIENT_DATA (n=0)"

def test_classify_verdict_insufficient_data_n_below_gate():
    v = _classify_verdict(eN=5, fN=10, wrDelta=3.0, fPnl=100, ePnl=50,
                          signal_type="gainers_early", n_gate=10)
    assert v == "INSUFFICIENT_DATA (n=5, need >=10)"

async def test_compute_signal_cohort_stats_uses_status_not_open_and_closed_at(db):
    """V27 MUST-FIX: query filters status != 'open' AND closed_at IN window,
    NOT status='closed' AND opened_at IN window. Seed mixed statuses
    (closed_tp, closed_sl, expired) and verify all count."""
    # Insert: 2 closed_tp wins + 1 closed_sl loss + 1 expired loss for gainers_early
    # All inside the [start, end) window via closed_at.
    # Plus 1 currently-open trade (status='open') — must NOT count.
    ...
    stats = await _compute_signal_cohort_stats(
        db, signal_type="gainers_early", start=..., end=...
    )
    assert stats["fN"] == 4  # 2 + 1 + 1, open trade excluded

async def test_compute_signal_cohort_stats_handles_zero_n(db):
    """V27 SHOULD-FIX: zero trades returns fN=0, eN=0, no division-by-zero."""
    stats = await _compute_signal_cohort_stats(
        db, signal_type="nonexistent", start=..., end=...
    )
    assert stats["fN"] == 0
    assert stats["eN"] == 0
    assert stats["fWr"] == 0
    assert stats["eWr"] is None
    assert stats["wrDelta"] is None
```

- [ ] **Step 2 — Run, expect fail**
- [ ] **Step 3 — Implement** in `scout/trading/cohort_digest.py`:

```python
"""Cohort digest builder — weekly would_be_live vs full-cohort comparison.

Verdict-classification rule mirrors dashboard/frontend/components/TradingTab.jsx
verbatim. See plan §Verdict-classification rule (locked) for the full spec.
"""

from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
import structlog
from scout.config import Settings
from scout.db import Database

log = structlog.get_logger()

_NEAR_IDENTICAL_COHORTS = ("chain_completed",)
_LIVE_ELIGIBLE_ENUMERATED_TYPES = (
    "chain_completed", "volume_spike", "gainers_early",
)


def _classify_verdict(
    *, eN: int, fN: int, wrDelta: float | None,
    fPnl: float, ePnl: float, signal_type: str, n_gate: int,
    strong_wr_gap_pp: float = 15.0,
    strong_pnl_floor_usd: float = 200.0,
    moderate_wr_gap_pp: float = 5.0,
) -> str:
    if signal_type in _NEAR_IDENTICAL_COHORTS:
        return "near-identical"
    if eN == 0:
        return "INSUFFICIENT_DATA (n=0)"
    if eN < n_gate:
        return f"INSUFFICIENT_DATA (n={eN}, need >={n_gate})"
    signFlipRaw = (fPnl > 0 and ePnl < 0) or (fPnl < 0 and ePnl > 0)
    strongPattern = (
        signFlipRaw
        and abs(fPnl) >= strong_pnl_floor_usd
        and abs(ePnl) >= strong_pnl_floor_usd
        and wrDelta is not None
        and abs(wrDelta) > strong_wr_gap_pp  # STRICT > per dashboard
    )
    if strongPattern:
        return "strong-pattern (exploratory)"
    if signFlipRaw or (wrDelta is not None and abs(wrDelta) > moderate_wr_gap_pp):
        return "moderate"
    return "tracking"


async def _compute_signal_cohort_stats(
    db: Database, *, signal_type: str, start: datetime, end: datetime
) -> dict:
    """Returns full-cohort and eligible-cohort stats for the given
    signal_type within [start, end). Mirrors dashboard/db.py:1148-1170:
        WHERE status != 'open' AND closed_at >= ? AND closed_at < ?
    Eligible cohort additionally filters `would_be_live = 1`.
    Wins are counted as `pnl_usd > 0` (V27 MUST-FIX — dashboard uses pnl_usd,
    not pnl_pct).

    Returns: dict(fN, fWins, fPnl, fWr, eN, eWins, ePnl, eWr, wrDelta) — None
    for eWr/wrDelta when eN=0 (no division-by-zero, V27 SHOULD-FIX).
    """
    # ... SELECT COUNT(*), SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END), SUM(pnl_usd)
    #     FROM paper_trades
    #     WHERE signal_type = ? AND status != 'open'
    #       AND closed_at >= ? AND closed_at < ?
    #     -- once unfiltered, once with AND would_be_live = 1
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

def test_detect_verdict_flip_emits_when_label_changes_with_both_n_above_gate():
    """V27 SHOULD-FIX: signal-records carry n_eligible so the n-gate guard
    can be asserted. moderate → strong-pattern with both eN >= 10 is a flip."""
    flips = _detect_verdict_flip(
        current={"gainers_early": {"verdict": "strong-pattern (exploratory)", "eN": 14}},
        previous={"gainers_early": {"verdict": "moderate", "eN": 12}},
        n_gate=10,
    )
    assert flips == [("gainers_early", "moderate", "strong-pattern (exploratory)")]

def test_detect_verdict_flip_ignores_when_either_n_below_gate():
    """V27 SHOULD-FIX: previous eN < n_gate means the previous-week
    verdict was already INSUFFICIENT_DATA — not a regime flip."""
    flips = _detect_verdict_flip(
        current={"gainers_early": {"verdict": "moderate", "eN": 12}},
        previous={"gainers_early": {"verdict": "tracking", "eN": 5}},
        n_gate=10,
    )
    assert flips == []

def test_detect_verdict_flip_ignores_insufficient_data_transitions():
    """INSUFFICIENT_DATA → moderate is NOT a flip (n-rate, not regime)."""
    flips = _detect_verdict_flip(
        current={"slow_burn": {"verdict": "moderate", "eN": 12}},
        previous={"slow_burn": {"verdict": "INSUFFICIENT_DATA (n=5, need >=10)", "eN": 5}},
        n_gate=10,
    )
    assert flips == []

def test_detect_verdict_flip_ignores_near_identical_chain_completed():
    """near-identical is a structural label, not a verdict — never flips."""
    flips = _detect_verdict_flip(
        current={"chain_completed": {"verdict": "near-identical", "eN": 50}},
        previous={"chain_completed": {"verdict": "moderate", "eN": 50}},
        n_gate=10,
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
| Final-window block date mis-match OR cron misses Monday 2026-06-08 (V28 SHOULD-FIX) | Low | Medium | Fire on `end_date >= COHORT_DIGEST_FINAL_DATE AND not last_final_block_fired` — the FIRST eligible run after the lock-date carries the decision block. `last_final_block_fired` persisted in `main.py` weekly-loop state (mirrors `last_cohort_digest_date`). Operator restart / VPS outage on the exact Monday does not lose the final alert. |
| Pre-existing weekly_digest fires on same day → operator sees 2 messages | Refuted (V28 NICE-TO-HAVE fold) | — | `FEEDBACK_WEEKLY_DIGEST_WEEKDAY=6` (Sunday); cohort_digest fires Monday (`COHORT_DIGEST_DAY_OF_WEEK=0`). Different days. Cadence-separation is intentional — Sunday PnL look-back + Monday cohort-comparison look-ahead. No collision. |

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
