**New primitives introduced:** Same as `tasks/plan_first_signal_retirement_decision.md` post V38/V39 fold — NONE. Pure decision artifact + operator-instruction block referencing existing `Database.revive_signal_with_baseline` helper.

# Design: BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION

**Plan reference:** `tasks/plan_first_signal_retirement_decision.md` (`e4bd974` + V38/V39 fold `f003b2b`)
**Cycle:** 9 of autonomous backlog knockdown.

## Hermes-first analysis

| Domain | Hermes skill found? | Decision |
|---|---|---|
| Auto-suspend audit-trail interpretation | None | Project-internal — `scout/trading/auto_suspend.py` is source of truth |
| Signal-revival decision frameworks | None | Project-internal trading decision |
| Database revival helper invocation | N/A — using existing in-tree `Database.revive_signal_with_baseline` | Reuse, don't reinvent |

awesome-hermes-agent: 404 (consistent). **Verdict:** custom decision doc using existing in-tree primitives only.

## Design decisions

### D1. Decision is REVIVE-AND-SOAK (Option A), not bare DEFER

Plan's V38 fold established the affirmative case: positive-tail edge ($45/winner × 44 trades = $2,003; drag concentrated in `closed_expired` time-decay). Bare DEFER (Option C) leaves the dispatcher path live and produces NO new evidence; REVIVE-AND-SOAK produces durable data either way (regression → clean RETIRE, or non-regression → KEEP-PAPER).

### D2. Operator action goes through `Database.revive_signal_with_baseline`

Per V38 MUST-FIX: raw SQL bypasses (a) cool-off check, (b) joint `tg_alert_eligible` restore via DEFAULT_ALLOW_SIGNALS lookup, (c) `BEGIN EXCLUSIVE` atomicity, (d) consistent audit row, (e) structlog observability hooks. The helper exists at `scout/db.py:4056` for exactly this case.

The PR ships the doc + the operator invocation block; the actual revival is operator-manual to preserve audit-trail integrity (`applied_by='operator_cycle9_manual'` distinguishes from auto-suspend's `'auto_suspend'` and from the helper's default `'operator'`).

### D3. Decision criteria are pre-registered + data-bound

Per CLAUDE.md §11 (data-bound, not calendar-bound). Threshold table in plan §Pre-registered re-evaluation criteria. Key gates:

- n ≥ 10 required before applying the verdict table; n < 10 auto-extends to 28d
- KEEP demands NON-REGRESSION (PnL ≥ 0 AND positive-tail win rate ≥ 17%), not spontaneous profitability
- Escalation threshold (live-roadmap revisit) sits HIGHER at PnL ≥ +$200 + avg winner ≥ +$30

### D4. Memory checkpoint anchors the future decision

`~/.claude/projects/C--projects-gecko-alpha/memory/project_first_signal_revival_decision_2026_05_31.md` records the revival query, the soak window, and the pre-registered criteria so the future-self at 2026-05-31 doesn't have to re-derive the threshold rationale.

### D5. No code change in this PR

Pure decision artifact. The plan correctly defers the actual revival to operator-manual execution. Build artifacts:

| File | Type | Content |
|---|---|---|
| `tasks/findings_first_signal_retirement_decision_2026_05_17.md` | NEW | Decision findings doc (mirrors plan §Investigation + §Why-keep-paper + §Pre-registered criteria) |
| `backlog.md` | MODIFY | Flip `BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION` to SHIPPED-WITH-DECISION |
| `~/.claude/.../memory/project_first_signal_revival_decision_2026_05_31.md` | NEW | Future-self memory checkpoint |

## Cross-file invariants

| Invariant | Source | Verification |
|---|---|---|
| Helper exists with current signature | `scout/db.py:4056-4108` | Read at audit time; signature: `revive_signal_with_baseline(self, signal_type, *, reason, operator, force=False, settings=None)` |
| Helper restores `tg_alert_eligible` via `DEFAULT_ALLOW_SIGNALS` | `scout/db.py:4204-4216` | `first_signal` ∉ `DEFAULT_ALLOW_SIGNALS` → `restored_to=0`; logged as `signal_revived_tg_eligible` |
| Combined-gate logic unchanged from PR #79 | `scout/trading/auto_suspend.py:236-237` | V38+V39 both verified |
| `signal_params_audit` schema supports the helper's audit row | `applied_at TEXT NOT NULL`, `applied_by TEXT NOT NULL` | Verified at q3 SSH query |
| `SIGNAL_REVIVAL_MIN_SOAK_DAYS` cool-off setting exists | `scout/config.py` | Helper reads `settings.SIGNAL_REVIVAL_MIN_SOAK_DAYS`; default 7 |

## Commit sequence (3 commits, bisect-safe)

1. `feat(audit): first_signal retirement decision findings (cycle 9 commit 1/3)` — `tasks/findings_first_signal_retirement_decision_2026_05_17.md`
2. `docs(backlog): close BL-NEW-FIRST-SIGNAL-RETIREMENT-DECISION (cycle 9 commit 2/3)` — backlog flip + memory checkpoint
3. (Optional, deferred) operator-side revival happens out-of-band post-merge

## Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Operator forgets to run revival post-merge | Medium | Low | Memory checkpoint reminds at 2026-05-31; findings doc has operator instruction block |
| Cool-off check trips on revival (first revival → impossible) | Refuted | — | `signal_params_audit` has only auto_suspend row + a `sync_to_env` row (operator) + `conviction_lock_enabled` change — no prior `enabled` revival, so the cool-off SELECT returns no row, no check fires |
| Helper signature changes before operator runs revival | Very Low | Low | Operator block cites current signature; deviations would be caught at runtime |
| Pre-existing `tg_alert_eligible=0` confuses interpretation | Refuted | — | Helper logs `restored_to=0` for first_signal explicitly; matches existing operator opt-out |
| 14d soak ambiguous outcome | Medium | Low | Verdict table includes EXTEND-SOAK + n<10 auto-extend trip-wire |

## Out of scope

- Changes to `auto_suspend.py` (current logic is correct)
- Changes to `revive_signal_with_baseline` (existing helper is correct for this use case)
- Changes to `FIRST_SIGNAL_MIN_SIGNAL_COUNT` admission gate
- Auto-revival policy (operator-manual is the right safety posture)

## Deployment

Doc-only PR. No service restart, no schema change. Operator runs the revival helper invocation manually post-merge if/when they greenlight Option A.
