# Plan: bounded suppression receipt inventory

**New primitives introduced:** NONE; docs-only findings from read-only inspection.

## Hermes-first analysis
| Domain | Hermes skill found? | Decision |
|---|---|---|
| Receipt/log inventory | Skills Hub fetched 2026-09-14; catalog stayed loading, no capability verified: https://hermes-agent.nousresearch.com/docs/skills | Reuse existing SSH/Python/source inspection; no dependency |
| Runtime evidence | Existing Gecko evidence contract and audit | Reuse; no new collector |

awesome-hermes-agent ecosystem checked at https://github.com/0xNyk/awesome-hermes-agent on 2026-09-14. Verdict: general orchestration listings do not supply Gecko receipts; no exhaustive absence claim.

## Scope and drift
Master 70462c16 includes PR588's evidence contract. DASH-08/11 bounded visibility already shipped; this inventories the contract's next independent-receipt candidates, not another health dashboard. The historical pool probe remains closed/parked. Claude safe-mode session 68e48766-6882-42fb-9393-642924930735 drafted the plan; coordinator narrowed it below.

Production preflight at 21:37:54Z found HEAD 77751890c9f1f51ed348c365d4e7a5985ea2827d and active pipeline/dashboard. These facts do not prove logging configuration or receipt completeness.

## Runtime assumptions and limits
Verify repo revision and source hashes for scout/main.py, scout/outcome_ledger.py, scout/trading/signals.py and scout/trading/decision_events.py before interpreting their runtime logs. Verify pipeline StandardOutput and active state. No settings/env reads: effective flags, levels and retention remain unknown.

One metadata command and one journal command, each bounded to 15 seconds on host. No DB queries, directory scans, backup reads, raw log exports, outbound vendor calls, config or runtime writes. Journal window pinned to explicit UTC start/end, last 200 entries maximum. Read at most 2 MiB from journal subprocess, kill on overflow/timeout and return incomplete. No retries to widen coverage.

Only allowlisted event names and allowlisted field names may leave the host, plus aggregate entry/parse/truncation counters and journal timestamps. Unknown event/field strings are counted, never printed. The reducer must suppress raw MESSAGE, values, exceptions, token IDs and URLs. Missing, capped or malformed evidence stays UNKNOWN. Source hashes mismatch: stop runtime interpretation and report drift.

## Source candidate hypotheses (to verify)
- outcome_ledger.py:590-596 logs ledger_emission_recorded after commit with ledger_id. It is a receipt copy, not an independent attempt.
- signals.py:66-67 and outcome_ledger.py:476-477 return on disabled flags without attempt records.
- decision_events.py logs its own event_id, not a shared ledger/decision key.
- main.py logging configuration may allow debug events; inspect rather than assume filtering.
- Label-pass aggregate logs do not establish selected price-observation lineage.

## Steps and deliverables
- [x] Fresh master, backlog/lessons/contract drift, initial runtime identity, Hermes check.
- [ ] Two parallel plan reviews; fold before design.
- [ ] Write design specifying reducer, allowlists and failure semantics; two parallel reviews; fold before execution.
- [ ] Execute bounded read-only preflight and journal reducer; preserve sanitized output locally.
- [ ] Build findings with source evidence, observed bounds, D1-D4 candidate classification and explicit exclusions. All contract verdicts remain UNKNOWN; candidate availability is not ACCEPTED.
- [ ] Open docs PR; two parallel independent PR reviews, folds and exact-head checks.

Files: this plan, tasks/design_suppression_receipt_inventory_2026_09_14.md, tasks/findings_suppression_receipt_inventory_2026_09_14.md, tasks/review_suppression_receipt_inventory_2026_09_14.md, a new checklist in tasks/todo.md, and PR-owned reviewer metadata after real reviews.

## Verification and next gate
Check source references, verify reducer with synthetic adversarial messages before use, inspect docs-only diff and run git diff --check. No application tests needed for prose. No claim of historical irrecoverability or full retained-hour coverage from this sample. Follow-on work is a separately reviewed targeted inventory or forward-evidence design, not activation. Rollback is a docs revert; no deployment.
