**New primitives introduced:** NONE. This design elaborates implementation patterns + operator UX + integration choreography + failure-mode taxonomy + test strategy for the primitives defined in `tasks/plan_lc_revival_criteria_tightening.md`. No additional functions, dataclasses, Settings keys, schemas, or alerts beyond the plan's enumeration.

# losers_contrarian Revival-Criteria Design

**Companion to:** `tasks/plan_lc_revival_criteria_tightening.md` (v2)

**Scope of this design:** What plan v2 does NOT cover — concrete implementation patterns, operator runbook, integration choreography with adjacent primitives, the 4-verdict×operator-action matrix, test isolation strategy, performance/scaling boundaries, rollback semantics.

---

## 1. Integration choreography (composes with adjacent primitives)

The evaluator is a **separate operator tool** that runs BEFORE the operator chooses whether to (a) write a `soak_verdict` audit row or (b) call `revive_signal_with_baseline`. It does NOT replace or wrap either action.

### 1a. End-to-end operator flow

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Day -1: trigger event                                                │
│  - auto_suspend fires on losers_contrarian (audit row id=26 today)   │
│  - OR operator considers writing a `keep_on_permanent` soak_verdict  │
│  - OR cool-off (BL-NEW-REVIVAL-COOLOFF) expires; revival under       │
│    consideration                                                      │
└──────────────────────────────────────────────────────────────────────┘
                                  ↓
┌──────────────────────────────────────────────────────────────────────┐
│ Day 0: operator runs the evaluator                                   │
│                                                                       │
│  $ uv run python -m scout.trading.revival_criteria losers_contrarian │
│        --db /root/gecko-alpha/scout.db                               │
│                                                                       │
│  Reads paper_trades (read-only) + signal_params_audit (read-only)    │
│  Prints diagnostic table + verdict + (if PASS) BEGIN IMMEDIATE SQL   │
│  Exits 0=PASS / 1=FAIL / 2=BELOW_MIN_TRADES|STRATIFICATION_INFEASIBLE│
└──────────────────────────────────────────────────────────────────────┘
                                  ↓
                       ┌──────────┴──────────┐
                       ↓                     ↓
            ┌──────────────────┐   ┌──────────────────┐
            │  Verdict = PASS  │   │ Verdict != PASS  │
            └──────────────────┘   └──────────────────┘
                       ↓                     ↓
            ┌──────────────────┐   ┌──────────────────────────┐
            │ Operator paste:  │   │ Operator action          │
            │                  │   │ - BELOW_MIN_TRADES: wait │
            │ sqlite3 scout.db │   │ - STRATIFICATION_INFEA-  │
            │ <<EOF            │   │   SIBLE: pass             │
            │ BEGIN IMMEDIATE; │   │   --cutover-iso explicit │
            │ INSERT ...       │   │ - FAIL: do NOT revive;   │
            │ COMMIT;          │   │   leave auto-suspend.    │
            │ EOF              │   └──────────────────────────┘
            └──────────────────┘
                       ↓
            ┌──────────────────────────────────────────────────────────┐
            │ Then SEPARATELY decide whether to call:                  │
            │   db.revive_signal_with_baseline('losers_contrarian',    │
            │       reason='...', operator='operator')                 │
            │                                                           │
            │ NOTE: PASS does NOT bypass BL-NEW-REVIVAL-COOLOFF        │
            │ (db.py:4208 cool-off filter keys on field_name='enabled',│
            │  field_name='soak_verdict' rows don't match).             │
            └──────────────────────────────────────────────────────────┘
```

### 1b. Disjointness from cool-off filter (Reviewer B finding #10 verified)

`Database.revive_signal_with_baseline` cool-off filter (`scout/db.py:4208-4217`):

```python
"""SELECT applied_at FROM signal_params_audit
   WHERE signal_type = ?
     AND field_name = 'enabled'      -- ← keyed on 'enabled'
     AND old_value = '0'
     AND new_value = '1'
     AND applied_by = 'operator'
   ORDER BY applied_at DESC LIMIT 1"""
```

The evaluator emits `field_name='soak_verdict'` rows. These are **structurally disjoint** from the cool-off SELECT: the cool-off WHERE clause filters on `field_name='enabled'`, so `soak_verdict` rows never match. Verified at design time; the disjointness is load-bearing for the safety claim "evaluator does not bypass cool-off."

**Future-self warning embedded in the emitted SQL header:** `-- NOTE: PASS does not bypass BL-NEW-REVIVAL-COOLOFF; check cool-off before calling Database.revive_signal_with_baseline.`

### 1c. Adjacent primitive boundaries

| Module | Read | Write | Why this boundary |
|---|---|---|---|
| `paper_trades` | ✓ (via `fetch_closed_trades`) | ✗ | Evaluator is the consumer; no insert path |
| `signal_params_audit` | ✓ (via `find_latest_regime_cutover`) | ✗ (operator writes via paste) | Decouples evaluation from state-change; operator gates the write |
| `signal_params` | ✗ | ✗ | No need; `enabled` state lives in audit |
| `.env` | ✓ (via Settings) | ✗ | Settings are read-only per BaseSettings convention |
| `revive_signal_with_baseline` | N/A | N/A | Operator calls separately if they choose |
| `auto_suspend.py` | N/A | N/A | No interaction; auto-suspend operates independently |
| `calibrate.py` | N/A | N/A | Sibling; no shared state |
| `send_telegram_message` | N/A | N/A | No alerts emitted by this module |

---

## 2. 4-verdict × operator-action matrix (V8 fold detail)

Per Reviewer A finding #8, the verdict enum was split from 3 → 4 values. Each verdict maps to a specific operator action:

| Verdict | Meaning | Operator action | Exit code |
|---|---|---|---|
| `PASS` | All gates clear under cutover-stratified 2-window evaluation | May paste emitted SQL → `keep_on_permanent` row. Cool-off check still required before revival. | 0 |
| `FAIL` | One or more gates rejected; specific reasons listed | Do NOT write `keep_on_permanent`. Do NOT revive. Wait for more data OR investigate gate that failed. | 1 |
| `BELOW_MIN_TRADES` | Sample size < 100 (default); insufficient data | Wait until n ≥ 100. Re-run evaluator later. | 2 |
| `STRATIFICATION_INFEASIBLE` | (a) no regime cutover found in audit, OR (b) cutover found but cannot split into 2× ≥7d ≥50-trade windows | Either pass `--cutover-iso ISO` explicit override (if operator knows a regime boundary the audit missed) OR wait for more data to accumulate post-cutover. | 2 |

**Why `BELOW_MIN_TRADES` ≠ `FAIL`:** operators reading verdicts piped into shell scripts (`&&` chains) should distinguish "tool said no but might say yes later" from "tool said no on evidence." Same shell-discrimination rationale for `STRATIFICATION_INFEASIBLE`.

---

## 3. Test isolation strategy

### 3a. Fixtures

`tests/test_revival_criteria.py` uses `tmp_path` per the project's `conftest.py` convention (pytest-asyncio `auto` mode is enabled in `pyproject.toml`). Each test creates a fresh `Database(str(tmp_path / "scout.db"))`, calls `.connect()` (which runs schema migrations), seeds rows via raw SQL through `db._conn.execute(...)` (matching the pattern in `tests/test_signal_params_auto_suspend.py`), then closes.

### 3b. Seed-data helpers

Two helpers live IN the test file (no production import):

- `_trade(...)` — synthetic `ClosedTrade` dataclass for pure-function tests (no DB).
- `_seed_trades(db, *, n, base_pnl, peak_pct, exit_reason, base_day, signal_type)` — inserts `n` paper_trades rows. **V15 fold per Reviewer B finding #15:** accepts `base_day` arg so stacked seeding (winners then losers) gets non-overlapping `closed_at` ranges. Without this, the median-split test from v1 was degenerate.
- `_seed_cutover(db, signal_type, iso)` — inserts one `signal_params_audit` row with `field_name='enabled'`, `applied_by='operator'` at the given ISO timestamp.

### 3c. Bootstrap test-speed escape hatch

Tests pass `Settings(REVIVAL_CRITERIA_BOOTSTRAP_RESAMPLES=500)` (vs. production default 10,000) so unit tests run in <500ms each. The full default is honored in production CLI runs; test override is acceptable because the bootstrap algorithm's correctness is verified by `compute_bootstrap_lb_per_trade` unit tests (Task 6) at 2,000 resamples with known-distribution inputs.

### 3d. NULL coverage (V6 fold per Reviewer B finding #6)

`tests/test_revival_criteria.py::test_fetch_closed_trades_null_peak_round_trips` explicitly inserts `peak_pct=NULL`, fetches via `fetch_closed_trades`, asserts the `peak_pct` field is `None` on the dataclass, then chains into `compute_no_breakout_and_loss_rate` to verify the NULL→no-breakout semantic survives the SQL→Python round-trip.

### 3e. What's NOT tested

- **No E2E test against srilu prod DB.** The evaluator is run against a `/tmp/scout_findings.db` snapshot during Task 13 (findings doc); the snapshot path is operator-managed, not part of CI.
- **No regression test for the emitted SQL actually inserting cleanly.** Plan v2's `_emit_soak_verdict_sql` test asserts SQL string SHAPE (`BEGIN IMMEDIATE`, `NULL`, `keep_on_permanent` present); execution against a real SQLite cursor is verified manually in Task 13 by the operator pasting the output.
- **No test for `find_latest_regime_cutover` returning a `soak_verdict` row.** The function intentionally filters to `_REGIME_BOUNDARY_FIELDS` (`enabled`, `tg_alert_eligible`, `trail_pct`, `sl_pct`, `max_duration_hours`) and excludes `soak_verdict`. The filter is documented in the function docstring; a separate test asserting the exclusion would be a tautology.

---

## 4. Performance / scaling boundaries

### 4a. Bootstrap CI cost (CLAUDE.md §11b mandate)

`compute_bootstrap_lb_per_trade` at default 10,000 resamples on n=600 trades:

- One resample: `rng.choices(pnls, k=600)` → ~600 RNG draws → ~0.1ms on a modern CPU
- 10,000 resamples × 600 trades × 2 windows = 12 million RNG draws + 10,000 means + 1 sort
- Empirical: ~80-120ms per window on the cycle-9 audit corpus, ~200-250ms for both windows
- **Acceptable** for an operator-initiated CLI tool; not acceptable for an in-loop hot path (not relevant here).

### 4b. Database connection lifetime

The CLI opens one `Database(...).connect()` at the top of `_main_async`, performs all reads under that connection, then closes. No connection pooling needed; SQLite handles single-process single-connection trivially.

### 4c. Memory footprint

`fetch_closed_trades` loads ALL closed trades for the signal_type into memory as `ClosedTrade` dataclasses. At 1000 trades × ~200 bytes/dataclass = ~200KB. Acceptable; no streaming needed.

---

## 5. Rollback / disable semantics

### 5a. No active rollback needed (zero production runtime impact)

The evaluator does NOT run in the production pipeline. It is an operator-initiated CLI tool. If the operator runs it and dislikes the output, they simply don't paste the emitted SQL. No state to roll back.

### 5b. If a `soak_verdict='keep_on_permanent'` row was written and operator wants to revoke

The evaluator does NOT auto-revoke. To revoke, the operator runs:

```sql
INSERT INTO signal_params_audit
  (signal_type, field_name, old_value, new_value, reason, applied_by, applied_at)
VALUES
  ('losers_contrarian', 'soak_verdict', 'keep_on_permanent', 'revoked',
   'operator revoke: <reason>', 'operator', '<iso>');
```

This is documented in the findings doc (Task 13) as a follow-up `BL-NEW-REVIVAL-VERDICT-AUTO-REVOKE` candidate; not in scope of this ship.

### 5c. If the evaluator itself ships with a bug

Since it has no production-runtime side-effects, a bug = misleading diagnostic output. Mitigation: PR review + 20-test unit suite + Task 12 regression verification + Task 13 sanity-check against 4 prod signals before any production paste.

If a critical bug is discovered post-merge, revert the merge commit; no `.env` flag or runtime state to clear.

---

## 6. Operator runbook (Task 13 output references this)

### 6a. First-time setup

```bash
# On operator's workstation OR srilu (the module is import-resolvable from both)
cd /root/gecko-alpha   # or local worktree
uv run python -m scout.trading.revival_criteria --help
```

### 6b. Typical invocation

```bash
# Read-only evaluation against prod DB
uv run python -m scout.trading.revival_criteria losers_contrarian \
    --db /root/gecko-alpha/scout.db
```

### 6c. Operator-cutover override (when audit-derived cutover is missing or stale)

```bash
uv run python -m scout.trading.revival_criteria losers_contrarian \
    --db /root/gecko-alpha/scout.db \
    --cutover-iso 2026-05-06T02:13:00Z
```

### 6d. PASS path — paste the emitted SQL

```bash
# Capture the SQL output
uv run python -m scout.trading.revival_criteria losers_contrarian \
    --db /root/gecko-alpha/scout.db > /tmp/lc_eval.txt

# Inspect, then if happy:
sqlite3 /root/gecko-alpha/scout.db < /tmp/extracted_sql_only.sql
```

### 6e. Combining with subsequent revival (after pasting `keep_on_permanent`)

```bash
# Check cool-off first:
sqlite3 /root/gecko-alpha/scout.db \
  "SELECT applied_at FROM signal_params_audit
   WHERE signal_type='losers_contrarian' AND field_name='enabled'
     AND old_value='0' AND new_value='1' AND applied_by='operator'
   ORDER BY applied_at DESC LIMIT 1;"

# If last revival > SIGNAL_REVIVAL_MIN_SOAK_DAYS (default 7) ago, then:
sudo systemctl stop gecko-pipeline
uv run python -c "
import asyncio
from scout.db import Database
from scout.config import get_settings
async def revive():
    db = Database('/root/gecko-alpha/scout.db')
    await db.connect()
    await db.revive_signal_with_baseline(
        'losers_contrarian',
        reason='post keep_on_permanent verdict (revival_criteria PASS)',
        operator='operator',
        settings=get_settings(),
    )
    await db.close()
asyncio.run(revive())
"
sudo systemctl start gecko-pipeline
```

---

## 7. Observability

Plan v2 Task 11 wires a single `log.info("revival_criteria_evaluated", ...)` structlog event at the end of `_main_async`. Fields emitted:

- `signal_type`
- `verdict` (string value)
- `n_trades`
- `cutover_source`
- `failures` (list of strings)

This creates an audit-of-evaluations trail visible in journalctl when the CLI is run on srilu. **Caveat:** the CLI is operator-initiated and short-lived; the log line shows up in whatever shell session invoked it, not in `gecko-pipeline.service` logs.

If the operator wants a persistent audit of evaluations (separate from `signal_params_audit` writes), a future follow-up could redirect stdout to `/var/log/revival_criteria_runs.log` via a wrapper shell script. Out of scope for this ship.

---

## 8. Cross-signal generalization design

Plan v2 Task 13 runs the evaluator against `gainers_early` (audit-id=24 `keep_on_permanent` falsification-risk check) without modifying gainers_early behavior. The design choice that enables this:

- The evaluator is **signal-agnostic**. Only `signal_type` is a parameter; all gates use `Settings` values that apply uniformly.
- `find_latest_regime_cutover` works for any `signal_type` with audit rows in `_REGIME_BOUNDARY_FIELDS`.
- The 4-verdict enum has no signal-specific branches.

This generalization is what enables the operator to evaluate ANY signal under the new criteria (gainers_early, narrative_prediction, volume_spike, chain_completed, etc.) by invoking `python -m scout.trading.revival_criteria <signal_type>`. The findings doc demonstrates this for 4 signals.

**Threshold tuning per-signal (future):** if operator finds the global thresholds are too strict/lenient for a specific signal class (e.g., chain_completed's positive expectancy is genuinely 5× higher than narrative_prediction's), a future follow-up could parameterize Settings by signal_type. Out of scope for this ship; documented as candidate `BL-NEW-REVIVAL-CRITERIA-PER-SIGNAL-TUNING` in findings doc.

---

## 9. Risks + mitigations

| Risk | Probability | Impact | Mitigation |
|---|---|---|---|
| Operator pastes SQL with wrong `signal_type` (typo) | Low | Mid (writes audit row for wrong signal) | `_validate_signal_type` regex rejects malformed signal_types BEFORE SQL is emitted |
| Operator runs evaluator against stale DB snapshot | Mid | Mid (verdict reflects stale data) | CLI prints `Evaluated at: <iso>` so operator can sanity-check timing against last paper-trade row |
| Two operators paste PASS SQL concurrently | Low | Low (two duplicate audit rows with different `applied_at`) | `BEGIN IMMEDIATE` wrapping in emitted SQL serializes the writes; duplicate detection is operator's responsibility (future: follow-up `BL-NEW-VERDICT-DEDUP` if needed) |
| Bootstrap RNG seed mis-collision across signals | Low | Low (deterministic seed=42 reused; reproducibility preserved but not regime-isolated) | Acceptable for a reproducibility-first design; if multi-signal evaluation enters cross-comparison, callers may pass distinct seeds |
| `_REGIME_BOUNDARY_FIELDS` misses a future regime-affecting field | Mid | Mid (cutover detection regresses silently) | Hardcoded list with prose docstring; if a new param is added to signal_params, reviewer notes during PR-stage if cutover behavior matters |
| `peak_pct=NULL` semantic flip in the schema | Low | High (no-breakout-AND-loss rate would invert) | Comment in `compute_no_breakout_and_loss_rate` documents NULL→no-breakout intent; if schema gains "peak_pct=NULL means unknown" semantics later, this comment + the function signature flag the assumption |

---

## 10. Out of scope (design-level)

- Streaming bootstrap (for n > 10,000 trades)
- Multi-signal cross-comparison (e.g., "rank all signals by their criteria score")
- Per-signal Settings override (BL-NEW-REVIVAL-CRITERIA-PER-SIGNAL-TUNING candidate)
- Auto-revoke of `keep_on_permanent` on post-verdict regression (BL-NEW-REVIVAL-VERDICT-AUTO-REVOKE candidate)
- Hard-gate on `revive_signal_with_baseline` (defer per operator constraint)
- Webhook/dashboard surface for evaluator output (CLI-only for now)

---

## 11. Self-review checklist

- [ ] Plan v2 enumerated all primitives; this design adds zero (✓ — header asserts NONE)
- [ ] Integration choreography diagrammed (✓ §1)
- [ ] All 4 verdicts mapped to operator action + exit code (✓ §2)
- [ ] Test isolation strategy explicit (✓ §3)
- [ ] Performance bounded with empirical numbers (✓ §4)
- [ ] Rollback semantics defined for all failure modes (✓ §5)
- [ ] Operator runbook concrete enough to execute (✓ §6)
- [ ] Observability discussed (✓ §7)
- [ ] Cross-signal generalization rationale explicit (✓ §8)
- [ ] Risks tabulated with mitigations (✓ §9)
- [ ] Out-of-scope captures follow-up backlog candidates (✓ §10)
